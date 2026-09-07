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

from notebooklm import NetworkError, ServerError
from notebooklm._app.artifacts import get_artifact, require_complete_artifact_listing
from notebooklm._app.generate import execute_generation
from notebooklm._app.generation_requests import AudioGenerationRequest
from notebooklm._app.research import execute_research_import
from notebooklm._web.rows.research_task import parse_research_task_models
from notebooklm.exceptions import ArtifactNotFoundError, RPCError
from notebooklm.outcomes import CommitState
from notebooklm.rpc import RPCMethod
from notebooklm.types import ArtifactListingComponent, ArtifactLookupStatus

from .common import ScenarioFailure, ScenarioResult
from .http import Disconnect, HttpFaultServer, Reply, Route
from .web import build_fault_client, list_response, rpc_response
from .web_generation_faults import BUDGETS as GENERATION_BUDGETS
from .web_generation_faults import IMPLEMENTATIONS as GENERATION_IMPLEMENTATIONS
from .web_generation_faults import REQUIRED_CHECKS as GENERATION_REQUIRED_CHECKS
from .web_generation_faults import SCENARIOS as GENERATION_SCENARIOS

_CREATE_ARTIFACT = Route.rpc(RPCMethod.CREATE_ARTIFACT.value)
_LIST_ARTIFACTS = Route.rpc(RPCMethod.LIST_ARTIFACTS.value)
_LIST_NOTES = Route.rpc(RPCMethod.GET_NOTES_AND_MIND_MAPS.value)
_POLL_RESEARCH = Route.rpc(RPCMethod.POLL_RESEARCH.value)
_IMPORT_RESEARCH = Route.rpc(RPCMethod.IMPORT_RESEARCH.value)
_GET_NOTEBOOK = Route.rpc(RPCMethod.GET_NOTEBOOK.value)
_LIST_LABELS = Route.rpc(RPCMethod.LIST_LABELS.value)
_CREATE_LABEL = Route.rpc(RPCMethod.CREATE_LABEL.value)
_READ = Route.rpc(RPCMethod.LIST_NOTEBOOKS.value)
_CLOSE_TIMEOUT = 2.0

SCENARIOS = (
    "workflow_artifact_incomplete_lookup",
    "workflow_generation_lost_kickoff",
    "workflow_generation_terminal_failure",
    "workflow_research_import_candidates",
    "workflow_research_import_ordered_loss",
    "workflow_collection_readback_failure",
    *GENERATION_SCENARIOS,
)


@asynccontextmanager
async def _cohort(
    result: ScenarioResult, server: HttpFaultServer, *, server_retries: int = 0
) -> AsyncIterator[Any]:
    client = build_fault_client(server, timeout=0.5, server_error_max_retries=server_retries)
    await server.__aenter__()
    primary: BaseException | None = None
    try:
        await client.__aenter__()
        yield client
    except BaseException as exc:
        primary = exc
        raise
    finally:
        close_errors: list[BaseException] = []
        for close in (lambda: client.close(drain=False), server.aclose):
            try:
                await asyncio.wait_for(close(), _CLOSE_TIMEOUT)
            except BaseException as exc:
                close_errors.append(exc)
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
            primary_error=None if primary is None else type(primary).__name__,
            close_errors=[type(exc).__name__ for exc in close_errors],
        )
        failed_check: ScenarioFailure | None = None
        for label, passed in (
            ("client_closed", not client._lifecycle.is_open()),
            ("handlers_drained", server.active_handlers == 0),
            ("server_clean", not server.errors and server.remaining() == 0),
            ("closed_without_error", not close_errors),
        ):
            try:
                result.require(label, passed)
            except ScenarioFailure as exc:
                failed_check = exc
        for exc in close_errors:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise exc
        if primary is None:
            if close_errors:
                raise close_errors[0]
            if failed_check is not None:
                raise failed_check


async def _resolve_notebook(_client: Any, notebook_id: str) -> str:
    return notebook_id


async def _resolve_sources(
    _client: Any, _notebook_id: str, source_ids: tuple[str, ...]
) -> list[str]:
    return list(source_ids)


def _request(*, wait: bool, timeout: float = 0.1) -> AudioGenerationRequest:
    return AudioGenerationRequest(
        notebook_id="nb-workflow",
        source_ids=(),
        wait=wait,
        timeout=timeout,
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


def _collection_list_response() -> bytes:
    return rpc_response(_LIST_LABELS.rpc_id or "", [None, [["Existing", None, "c-existing", ""]]])


def _completed_research_poll() -> tuple[str, bytes, tuple[str, ...]]:
    fixture = Path(__file__).parents[1] / "unit/fixtures/research_poll_task_metadata.json"
    polls = json.loads(fixture.read_text(encoding="utf-8"))["polls"]
    payload = polls[2]
    (task,) = parse_research_task_models(payload)
    urls = tuple(source.url for source in task.sources if source.url)
    return task.task_id, rpc_response(_POLL_RESEARCH.rpc_id or "", payload), urls


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
                _request(wait=True, timeout=5.0),
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
    task_id, poll, (source_url, *_rest) = _completed_research_poll()
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


async def _research_import_ordered_loss(result: ScenarioResult) -> None:
    task_id, poll, (first_url, second_url, *_rest) = _completed_research_poll()
    server = HttpFaultServer()
    server.enqueue(_POLL_RESEARCH, Reply(body=poll), Reply(body=poll))
    server.enqueue(
        _GET_NOTEBOOK,
        Reply(body=_source_list_response()),
        Reply(body=_source_list_response("imported-first", url=first_url)),
        Reply(body=_source_list_response("candidate-second", url=second_url)),
        Reply(body=_source_list_response("candidate-second", url=second_url)),
    )
    server.enqueue(
        _IMPORT_RESEARCH,
        Reply(body=rpc_response(_IMPORT_RESEARCH.rpc_id or "", [[["imported-first"], "First"]])),
        Disconnect(commit_id="research-import-later"),
    )
    error: BaseException | None = None
    async with _cohort(result, server) as client:
        first = await execute_research_import(client, "nb-workflow", task_id, oneshot=False)
        try:
            await execute_research_import(client, "nb-workflow", task_id, oneshot=False)
        except Exception as exc:  # first operation remains settled after the later failure
            error = exc
        recovered = await client.sources.list("nb-workflow")
    candidates = tuple(getattr(error, "reconciliation_candidates", ()))
    result.record(
        "workflow_outcome",
        first_imported=[entry["id"] for entry in first.imported.newly_imported],
        later_error=None if error is None else type(error).__name__,
        later_candidates=len(candidates),
        recovery_ids=[source.id for source in recovered],
    )
    result.require(
        "research_ordered_first_succeeds",
        first.imported.newly_imported == [{"id": "imported-first", "title": "First"}],
    )
    result.require("research_ordered_later_fails", isinstance(error, NetworkError))
    result.require(
        "research_ordered_no_replay",
        len([r for r in server.journal if r.route == _IMPORT_RESEARCH]) == 2,
    )
    result.require("research_ordered_later_candidate", candidates == ("candidate-second",))
    result.require(
        "research_ordered_recovery", [source.id for source in recovered] == ["candidate-second"]
    )


async def _collection_readback_failure(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _LIST_LABELS,
        Reply(body=_collection_list_response()),
        Reply(body=_collection_list_response()),
        Reply(503),
        Reply(body=_collection_list_response()),
    )
    server.enqueue(_CREATE_LABEL, Reply(body=rpc_response(_CREATE_LABEL.rpc_id or "", None)))
    error: BaseException | None = None
    async with _cohort(result, server) as client:
        preflight = await client.collections.list()
        try:
            await client.collections.create("Research")
        except Exception as exc:
            error = exc
        recovered = await client.collections.list()
    metadata = getattr(error, "operation_metadata", None)
    entries = () if metadata is None else metadata.entries
    result.record("workflow_outcome", error=None if error is None else type(error).__name__)
    result.require(
        "collection_preflight_decoded", [item.id for item in preflight] == ["c-existing"]
    )
    result.require("collection_readback_error", isinstance(error, ServerError))
    result.require(
        "collection_primary_confirmed",
        getattr(error, "commit_state", None) is CommitState.CONFIRMED,
    )
    result.require(
        "collection_readback_failed",
        len(entries) == 2 and entries[1].commit_state is CommitState.UNKNOWN,
    )
    result.require(
        "collection_one_create", len([r for r in server.journal if r.route == _CREATE_LABEL]) == 1
    )
    result.require("collection_recovery", [item.id for item in recovered] == ["c-existing"])


async def _artifact_incomplete_lookup(result: ScenarioResult) -> None:
    """An unavailable secondary read cannot establish absence or uniqueness.

    Studio and legacy note-row fixture shapes come from artifact completeness
    and note-service unit tests; the baseline runs both real wire decoders.
    """
    server = HttpFaultServer()
    studio = Reply(
        body=rpc_response(
            _LIST_ARTIFACTS.rpc_id or "", [[["studio-fixture", "Studio result", 2, None, 3]]]
        )
    )
    notes = Reply(
        body=rpc_response(
            _LIST_NOTES.rpc_id or "",
            [[["map-fixture", json.dumps({"name": "Mind map", "children": []})]]],
        )
    )
    server.enqueue(_LIST_ARTIFACTS, *[studio for _ in range(6)])
    server.enqueue(_LIST_NOTES, notes, *[Reply(503) for _ in range(4)], notes)
    errors: list[Exception] = []
    async with _cohort(result, server, server_retries=0) as client:
        baseline = await client.artifacts.list_with_status("nb-workflow")
        result.require("aggregate_baseline_complete", baseline.is_complete)
        result.require(
            "both_backings_decoded",
            {item.id for item in baseline.items} == {"studio-fixture", "map-fixture"},
        )
        missing = await client.artifacts.lookup("nb-workflow", "absent")
        positive = await client.artifacts.lookup("nb-workflow", "studio-fixture")
        for operation in (
            lambda: get_artifact(client, "nb-workflow", "absent"),
            lambda: require_complete_artifact_listing(client, "nb-workflow"),
        ):
            try:
                await operation()
            except Exception as exc:
                errors.append(exc)
        recovered = await client.artifacts.lookup("nb-workflow", "absent")
        result.require(
            "incomplete_miss_unknown",
            missing.status is ArtifactLookupStatus.UNKNOWN and missing.artifact is None,
        )
        result.require(
            "missing_secondary_component_evidenced",
            len(missing.failures) == 1
            and missing.failures[0].component is ArtifactListingComponent.NOTE_BACKED_MIND_MAPS
            and missing.failures[0].error_type == "ServerError",
        )
        result.require(
            "positive_hit_preserved",
            positive.status is ArtifactLookupStatus.FOUND
            and positive.artifact is not None
            and positive.artifact.id == "studio-fixture"
            and len(positive.failures) == 1,
        )
        result.require(
            "strict_projections_reject_incomplete_inventory",
            len(errors) == 2
            and all(
                isinstance(exc, RPCError) and not isinstance(exc, ArtifactNotFoundError)
                for exc in errors
            ),
        )
        result.require(
            "recovery_authoritative_missing",
            recovered.status is ArtifactLookupStatus.MISSING and not recovered.failures,
        )
        result.record(
            "workflow_outcome",
            lookup_status=missing.status.value,
            positive_status=positive.status.value,
            unavailable_components=[failure.component.value for failure in missing.failures],
            strict_error_types=[type(exc).__name__ for exc in errors],
            recovery_status=recovered.status.value,
        )
    result.require(
        "exact_primary_and_secondary_reads",
        sum(r.route == _LIST_ARTIFACTS for r in server.journal) == 6
        and sum(r.route == _LIST_NOTES for r in server.journal) == 6,
    )


_IMPLEMENTATIONS: dict[str, Callable[[ScenarioResult], Awaitable[None]]] = {
    "workflow_artifact_incomplete_lookup": _artifact_incomplete_lookup,
    "workflow_generation_lost_kickoff": _lost_kickoff,
    "workflow_generation_terminal_failure": _terminal_failure,
    "workflow_research_import_candidates": _research_import_candidates,
    "workflow_research_import_ordered_loss": _research_import_ordered_loss,
    "workflow_collection_readback_failure": _collection_readback_failure,
}


_REQUIRED_CHECKS: dict[str, list[str]] = {
    "workflow_artifact_incomplete_lookup": [
        "aggregate_baseline_complete",
        "both_backings_decoded",
        "client_closed",
        "closed_without_error",
        "exact_primary_and_secondary_reads",
        "handlers_drained",
        "incomplete_miss_unknown",
        "missing_secondary_component_evidenced",
        "positive_hit_preserved",
        "recovery_authoritative_missing",
        "server_clean",
        "strict_projections_reject_incomplete_inventory",
    ],
    "workflow_collection_readback_failure": [
        "client_closed",
        "closed_without_error",
        "collection_one_create",
        "collection_preflight_decoded",
        "collection_primary_confirmed",
        "collection_readback_error",
        "collection_readback_failed",
        "collection_recovery",
        "handlers_drained",
        "server_clean",
    ],
    "workflow_generation_lost_kickoff": [
        "client_closed",
        "closed_without_error",
        "handlers_drained",
        "lost_kickoff_network_error",
        "lost_kickoff_no_poll",
        "lost_kickoff_one_commit",
        "lost_kickoff_one_send",
        "lost_kickoff_recovery",
        "lost_kickoff_unconfirmed",
        "server_clean",
    ],
    "workflow_generation_terminal_failure": [
        "client_closed",
        "closed_without_error",
        "handlers_drained",
        "server_clean",
        "terminal_failure_one_kickoff",
        "terminal_failure_one_poll",
        "terminal_failure_recovery",
        "terminal_failure_task_id",
        "terminal_failure_typed_outcome",
    ],
    "workflow_research_import_candidates": [
        "client_closed",
        "closed_without_error",
        "handlers_drained",
        "research_import_candidate_only",
        "research_import_committed_once",
        "research_import_one_mutation",
        "research_import_original_error",
        "research_import_recovery",
        "research_import_unconfirmed",
        "server_clean",
    ],
    "workflow_research_import_ordered_loss": [
        "client_closed",
        "closed_without_error",
        "handlers_drained",
        "research_ordered_first_succeeds",
        "research_ordered_later_candidate",
        "research_ordered_later_fails",
        "research_ordered_no_replay",
        "research_ordered_recovery",
        "server_clean",
    ],
}
_IMPLEMENTATIONS.update(GENERATION_IMPLEMENTATIONS)


async def run_scenario(
    name: str, *, operation_id: str, result: ScenarioResult | None = None
) -> ScenarioResult:
    implementation = _IMPLEMENTATIONS.get(name)
    if implementation is None:
        raise ValueError(f"unknown Web workflow scenario {name!r}")
    result = result or ScenarioResult("web", name, operation_id)
    result.record(
        "plan",
        faults=[name],
        cohort_ids=[f"{operation_id}:0"],
        family="R10" if name.startswith("workflow_generation") else "R11",
        budgets=GENERATION_BUDGETS.get(
            name,
            {"scenario_timeout_s": 20.0, "rpc_timeout_s": 0.5, "cleanup_timeout_s": 2.0},
        ),
        required_checks=list(
            GENERATION_REQUIRED_CHECKS[name]
            if name in GENERATION_REQUIRED_CHECKS
            else _REQUIRED_CHECKS[name]
        ),
    )
    if name == "workflow_artifact_incomplete_lookup":
        result.record(
            "aggregate_plan",
            operations=[
                "artifacts.list_with_status",
                "artifacts.lookup",
                "get_artifact",
                "require_complete_artifact_listing",
            ],
            server_error_max_retries=0,
            expected_dispatches={"studio": 6, "notes": 6},
            faults=["secondary:503"] * 4,
        )
    await implementation(result)
    return result


__all__ = ["SCENARIOS", "run_scenario"]
