"""First-party Web workflow fault cohorts.

These scenarios keep the workflow boundary above the public client: the
generation executor owns kickoff-plus-polling rather than a test reproducing
that sequence with direct RPC calls.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from notebooklm import NetworkError
from notebooklm._app.generate import execute_generation
from notebooklm._app.generation_requests import AudioGenerationRequest
from notebooklm._app.research import execute_research_import
from notebooklm._web.rows.research_task import parse_research_task_models
from notebooklm.rpc import RPCMethod

from .common import ScenarioResult
from .http import Disconnect, HttpFaultServer, Reply, Route
from .web import build_fault_client, list_response, rpc_response

_CREATE_ARTIFACT = Route.rpc(RPCMethod.CREATE_ARTIFACT.value)
_LIST_ARTIFACTS = Route.rpc(RPCMethod.LIST_ARTIFACTS.value)
_POLL_RESEARCH = Route.rpc(RPCMethod.POLL_RESEARCH.value)
_IMPORT_RESEARCH = Route.rpc(RPCMethod.IMPORT_RESEARCH.value)
_GET_NOTEBOOK = Route.rpc(RPCMethod.GET_NOTEBOOK.value)
_READ = Route.rpc(RPCMethod.LIST_NOTEBOOKS.value)
_CLOSE_TIMEOUT = 2.0

SCENARIOS = (
    "workflow_generation_lost_kickoff",
    "workflow_generation_terminal_failure",
    "workflow_research_import_candidates",
)


@asynccontextmanager
async def _cohort(result: ScenarioResult, server: HttpFaultServer) -> AsyncIterator[Any]:
    client = build_fault_client(server, timeout=0.5, server_error_max_retries=0)
    await server.__aenter__()
    await client.__aenter__()
    try:
        yield client
    finally:
        await asyncio.wait_for(client.close(drain=False), _CLOSE_TIMEOUT)
        await asyncio.wait_for(server.aclose(), _CLOSE_TIMEOUT)
        result.record(
            "http_trace",
            requests=[
                {
                    "sequence": item.sequence,
                    "rpc_id": item.route.rpc_id,
                    "action": item.action,
                }
                for item in server.journal
            ],
            committed=list(server.committed),
        )
        result.record(
            "cleanup",
            client_closed=not client._lifecycle.is_open(),
            active_handlers=server.active_handlers,
            server_errors=list(server.errors),
            remaining_actions=server.remaining(),
        )
        result.require("client_closed", not client._lifecycle.is_open())
        result.require("handlers_drained", server.active_handlers == 0)
        result.require("server_clean", not server.errors and server.remaining() == 0)


async def _resolve_notebook(_client: Any, notebook_id: str) -> str:
    return notebook_id


async def _resolve_sources(
    _client: Any, _notebook_id: str, source_ids: tuple[str, ...]
) -> list[str]:
    return list(source_ids)


def _request(*, wait: bool) -> AudioGenerationRequest:
    return AudioGenerationRequest(
        notebook_id="nb-workflow",
        source_ids=(),
        wait=wait,
        timeout=0.1,
        interval=0.001,
    )


def _source_list_response(source_id: str | None = None, *, url: str = "") -> bytes:
    sources: list[Any] = []
    if source_id is not None:
        sources = [
            [
                [source_id],
                "Candidate",
                [None, None, None, None, 5, None, None, [url]],
                [None, 2],
            ]
        ]
    return rpc_response(_GET_NOTEBOOK.rpc_id or "", [["Notebook", sources, "nb-workflow"]])


def _completed_research_poll() -> tuple[str, bytes, str]:
    fixture = Path(__file__).parents[1] / "unit/fixtures/research_poll_task_metadata.json"
    polls = json.loads(fixture.read_text(encoding="utf-8"))["polls"]
    payload = polls[2]
    (task,) = parse_research_task_models(payload)
    source = next(source for source in task.sources if source.url)
    return task.task_id, rpc_response(_POLL_RESEARCH.rpc_id or "", payload), source.url


async def _lost_kickoff(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(_CREATE_ARTIFACT, Disconnect(commit_id="task-2"))
    server.enqueue(
        _READ,
        Reply(body=list_response(_READ.rpc_id or "", [("nb-recovered", "Recovered")])),
    )
    error: BaseException | None = None
    async with _cohort(result, server) as client:
        try:
            await execute_generation(
                _request(wait=True),
                client,
                notebook_resolver=_resolve_notebook,
                source_resolver=_resolve_sources,
            )
        except Exception as exc:  # exact public class asserted below
            error = exc
        recovered = await client.notebooks.list()
    result.record(
        "workflow_outcome",
        error=None if error is None else type(error).__name__,
        recovery_ids=[item.id for item in recovered],
    )
    result.require("lost_kickoff_network_error", isinstance(error, NetworkError))
    result.require("lost_kickoff_unconfirmed", bool(getattr(error, "unconfirmed", False)))
    result.require("lost_kickoff_one_send", len(server.journal) == 2)
    result.require("lost_kickoff_one_commit", server.committed == ["task-2"])
    result.require(
        "lost_kickoff_no_poll", not any(r.route == _LIST_ARTIFACTS for r in server.journal)
    )
    result.require("lost_kickoff_recovery", [item.id for item in recovered] == ["nb-recovered"])


async def _terminal_failure(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _CREATE_ARTIFACT,
        Reply(
            body=rpc_response(
                _CREATE_ARTIFACT.rpc_id or "",
                [["task-3", "Audio", 1, None, 1]],
            )
        ),
    )
    server.enqueue(
        _LIST_ARTIFACTS,
        Reply(
            body=rpc_response(
                _LIST_ARTIFACTS.rpc_id or "",
                [[["task-3", "Audio", 1, None, 4]]],
            )
        ),
    )
    server.enqueue(
        _READ,
        Reply(body=list_response(_READ.rpc_id or "", [("nb-recovered", "Recovered")])),
    )
    async with _cohort(result, server) as client:
        execution = await execute_generation(
            _request(wait=True),
            client,
            notebook_resolver=_resolve_notebook,
            source_resolver=_resolve_sources,
        )
        recovered = await client.notebooks.list()
    outcome = execution.generation
    result.record(
        "workflow_outcome",
        status=None if outcome is None else outcome.status,
        task_id=None if outcome is None else outcome.task_id,
        recovery_ids=[item.id for item in recovered],
    )
    result.require(
        "terminal_failure_typed_outcome", outcome is not None and outcome.status == "failed"
    )
    result.require("terminal_failure_task_id", outcome is not None and outcome.task_id == "task-3")
    result.require(
        "terminal_failure_one_kickoff",
        len([r for r in server.journal if r.route == _CREATE_ARTIFACT]) == 1,
    )
    result.require(
        "terminal_failure_one_poll",
        len([r for r in server.journal if r.route == _LIST_ARTIFACTS]) == 1,
    )
    result.require("terminal_failure_recovery", [item.id for item in recovered] == ["nb-recovered"])


async def _research_import_candidates(result: ScenarioResult) -> None:
    task_id, poll, source_url = _completed_research_poll()
    server = HttpFaultServer()
    server.enqueue(_POLL_RESEARCH, Reply(body=poll))
    server.enqueue(
        _GET_NOTEBOOK,
        Reply(body=_source_list_response()),
        Reply(body=_source_list_response("candidate-a", url=source_url)),
        Reply(body=_source_list_response("candidate-a", url=source_url)),
    )
    server.enqueue(_IMPORT_RESEARCH, Disconnect(commit_id="research-import"))
    error: BaseException | None = None
    async with _cohort(result, server) as client:
        try:
            await execute_research_import(client, "nb-workflow", task_id, oneshot=False)
        except Exception as exc:  # exact contract asserted after cleanup
            error = exc
        recovered = await client.sources.list("nb-workflow")
    candidates = tuple(getattr(error, "reconciliation_candidates", ()))
    result.record(
        "workflow_outcome",
        error=None if error is None else type(error).__name__,
        unconfirmed=bool(getattr(error, "unconfirmed", False)),
        candidate_count=len(candidates),
        recovery_ids=[source.id for source in recovered],
    )
    result.require("research_import_original_error", isinstance(error, NetworkError))
    result.require("research_import_unconfirmed", bool(getattr(error, "unconfirmed", False)))
    result.require(
        "research_import_one_mutation",
        len([r for r in server.journal if r.route == _IMPORT_RESEARCH]) == 1,
    )
    result.require("research_import_committed_once", server.committed == ["research-import"])
    result.require("research_import_candidate_only", candidates == ("candidate-a",))
    result.require(
        "research_import_recovery", [source.id for source in recovered] == ["candidate-a"]
    )


_IMPLEMENTATIONS: dict[str, Callable[[ScenarioResult], Awaitable[None]]] = {
    "workflow_generation_lost_kickoff": _lost_kickoff,
    "workflow_generation_terminal_failure": _terminal_failure,
    "workflow_research_import_candidates": _research_import_candidates,
}


async def run_scenario(
    name: str, *, operation_id: str, result: ScenarioResult | None = None
) -> ScenarioResult:
    implementation = _IMPLEMENTATIONS.get(name)
    if implementation is None:
        raise ValueError(f"unknown Web workflow scenario {name!r}")
    result = result or ScenarioResult("web", name, operation_id)
    result.record(
        "plan",
        family="R11" if name.startswith("workflow_research") else "R10",
        budgets={"rpc_timeout_s": 0.5},
    )
    await implementation(result)
    return result


__all__ = ["SCENARIOS", "run_scenario"]
