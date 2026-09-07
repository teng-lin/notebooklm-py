"""Successful wire responses whose application data cannot establish completion."""

from __future__ import annotations

import asyncio

from notebooklm._app.artifacts import get_artifact
from notebooklm._app.generate import execute_generation
from notebooklm.exceptions import ArtifactNotFoundError, DecodingError, RPCError
from notebooklm.outcomes import CommitState
from notebooklm.types import ArtifactLookupStatus

from .common import ScenarioResult
from .http import HttpFaultServer, Reply, Route, Stall
from .web import rpc_response
from .web_concurrency import _audio_row
from .web_workflows import (
    _CREATE_ARTIFACT,
    _LIST_ARTIFACTS,
    _LIST_NOTES,
    _cohort,
    _request,
    _resolve_notebook,
    _resolve_sources,
)


def _reply(route: Route, payload: object) -> Reply:
    return Reply(body=rpc_response(route.rpc_id or "", payload))


async def missing_generation_id(result: ScenarioResult) -> None:
    """Adversarial kickoff has no identity; never invent a polling prerequisite."""
    server = HttpFaultServer()
    server.enqueue(
        _CREATE_ARTIFACT,
        _reply(_CREATE_ARTIFACT, [["", "Audio", 1, None, 1]]),
        _reply(_CREATE_ARTIFACT, [["task-recovered", "Audio", 1, None, 1]]),
    )
    server.enqueue(_LIST_ARTIFACTS, _reply(_LIST_ARTIFACTS, [[_audio_row("task-recovered")]]))
    async with _cohort(result, server) as client:
        failure = None
        outcome = None
        try:
            outcome = await execute_generation(
                _request(wait=True),
                client,
                notebook_resolver=_resolve_notebook,
                source_resolver=_resolve_sources,
            )
        except RPCError as error:
            failure = error
        result.record(
            "missing_identity",
            error=None if failure is None else type(failure).__name__,
            status=None if outcome is None else outcome.generation.status,
        )
        result.require("missing_id_rejected", isinstance(failure, DecodingError))
        result.require(
            "missing_commit_unknown",
            failure is not None and failure.commit_state is CommitState.UNKNOWN,
        )
        result.require(
            "no_invented_poll", [row.route for row in server.journal] == [_CREATE_ARTIFACT]
        )
        recovered = await execute_generation(
            _request(wait=True),
            client,
            notebook_resolver=_resolve_notebook,
            source_resolver=_resolve_sources,
        )
        result.require(
            "generation_recovered",
            recovered.generation.status == "completed"
            and recovered.generation.task_id == "task-recovered",
        )
        result.require(
            "kickoffs_not_replayed",
            sum(row.route == _CREATE_ARTIFACT for row in server.journal) == 2,
        )


async def empty_incomplete_lookup(result: ScenarioResult) -> None:
    """An empty successful constituent cannot erase the other constituent's failure."""
    server = HttpFaultServer()
    server.enqueue(_LIST_ARTIFACTS, *[_reply(_LIST_ARTIFACTS, [[]]) for _ in range(3)])
    server.enqueue(_LIST_NOTES, Reply(503), Reply(503), _reply(_LIST_NOTES, [[]]))
    async with _cohort(result, server) as client:
        lookup = await client.artifacts.lookup("nb-workflow", "absent")
        result.require(
            "empty_plus_failure_unknown",
            lookup.status is ArtifactLookupStatus.UNKNOWN
            and lookup.artifact is None
            and len(lookup.failures) == 1,
        )
        error = None
        try:
            await get_artifact(client, "nb-workflow", "absent")
        except RPCError as exc:
            error = exc
        result.require(
            "strict_miss_not_authoritative",
            error is not None and not isinstance(error, ArtifactNotFoundError),
        )
        recovered = await client.artifacts.lookup("nb-workflow", "absent")
        result.require(
            "complete_empty_authoritative",
            recovered.status is ArtifactLookupStatus.MISSING and not recovered.failures,
        )
        result.require("aggregate_reads_bounded", len(server.journal) == 6)
        result.record(
            "aggregate_outcome",
            status=lookup.status.value,
            failures=len(lookup.failures),
            recovery=recovered.status.value,
        )


async def completed_without_media(result: ScenarioResult) -> None:
    """Known upstream ordering: terminal status precedes usable media URLs."""
    server = HttpFaultServer()
    server.enqueue(
        _LIST_ARTIFACTS,
        _reply(_LIST_ARTIFACTS, [[["task-media", "Audio", 1, None, 3]]]),
        Stall("headers", "media-ready", _reply(_LIST_ARTIFACTS, [[_audio_row("task-media")]])),
        _reply(_LIST_ARTIFACTS, [[_audio_row("task-media")]]),
    )
    task = None
    gate_wait = None
    statuses = []
    primary: BaseException | None = None
    async with _cohort(result, server) as client:
        try:
            task = asyncio.create_task(
                client.artifacts.wait_for_completion(
                    "nb-workflow",
                    "task-media",
                    timeout=3,
                    initial_interval=0.001,
                    on_status_change=lambda status: statuses.append(status.status),
                )
            )
            gate_wait = asyncio.create_task(server.wait_for_gate("media-ready", timeout=2))
            await asyncio.wait((task, gate_wait), return_when=asyncio.FIRST_COMPLETED)
            result.require("media_poll_continued", gate_wait.done() and not task.done())
            await gate_wait
            result.require(
                "incomplete_media_not_terminal", not task.done() and statuses == ["in_progress"]
            )
            server.release("media-ready")
            completed = await asyncio.wait_for(task, 3)
            result.require("media_completion_after_release", completed.is_complete)
            recovered = await client.artifacts.poll_status("nb-workflow", "task-media")
            result.require("media_poll_recovered", recovered.is_complete)
            result.require("media_polls_bounded", len(server.journal) == 3)
            result.record("poll_progress", transitions=statuses, polls=len(server.journal))
        except BaseException as error:
            primary = error
            raise
        finally:
            server.release("media-ready")
            owned = [item for item in (task, gate_wait) if item is not None]
            for item in owned:
                if not item.done():
                    item.cancel()
            cleanup_error: BaseException | None = None
            try:
                if owned:
                    await asyncio.wait(owned, timeout=2)
            except BaseException as error:
                cleanup_error = error
            pending = [item for item in owned if not item.done()]
            settled_errors = [
                type(item.exception()).__name__
                for item in owned
                if item.done() and not item.cancelled() and item.exception() is not None
            ]
            result.record(
                "poll_cleanup",
                pending_tasks=len(pending),
                settled_error_types=settled_errors,
                cleanup_error_type=None if cleanup_error is None else type(cleanup_error).__name__,
                primary_error_type=None if primary is None else type(primary).__name__,
            )
            if primary is None:
                if cleanup_error is not None:
                    raise cleanup_error
                result.require("poll_owned_tasks_settled", not pending)


IMPLEMENTATIONS = {
    "consistency_missing_generation_id": missing_generation_id,
    "consistency_empty_incomplete_lookup": empty_incomplete_lookup,
    "consistency_completed_without_media": completed_without_media,
}
REQUIRED_CHECKS = {
    "consistency_missing_generation_id": (
        "missing_id_rejected",
        "missing_commit_unknown",
        "no_invented_poll",
        "generation_recovered",
        "kickoffs_not_replayed",
    ),
    "consistency_empty_incomplete_lookup": (
        "empty_plus_failure_unknown",
        "strict_miss_not_authoritative",
        "complete_empty_authoritative",
        "aggregate_reads_bounded",
    ),
    "consistency_completed_without_media": (
        "media_poll_continued",
        "incomplete_media_not_terminal",
        "media_completion_after_release",
        "media_poll_recovered",
        "media_polls_bounded",
        "poll_owned_tasks_settled",
    ),
}
for _name in REQUIRED_CHECKS:
    REQUIRED_CHECKS[_name] += (
        "client_closed",
        "handlers_drained",
        "server_clean",
        "closed_without_error",
    )

BUDGETS = {
    "consistency_missing_generation_id": {
        "scenario_timeout_s": 8,
        "cleanup_timeout_s": 2,
        "max_requests": 3,
        "max_commits": 2,
    },
    "consistency_empty_incomplete_lookup": {
        "scenario_timeout_s": 8,
        "cleanup_timeout_s": 2,
        "max_requests": 6,
        "max_commits": 0,
    },
    "consistency_completed_without_media": {
        "scenario_timeout_s": 8,
        "cleanup_timeout_s": 2,
        "max_requests": 3,
        "max_commits": 0,
    },
}
