"""Drive-staged Android imports with conservative cleanup and commit evidence."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import time
from pathlib import Path
from typing import Any

import grpc

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
    sources_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.v1 import (
    source_settings_pb2 as settings,
)
from notebooklm.exceptions import AuthError, RPCTimeoutError, SourceAddError, SourceProcessingError

from .android import build_android_client
from .android_cleanup import android_cohort
from .common import ScenarioResult
from .grpc import (
    ADD_SOURCES,
    ADD_TENTATIVE_SOURCES,
    GET_PROJECT,
    GrpcFaultServer,
    abort,
    commit_abort,
    reply,
    wait_reply,
)
from .http import HttpFaultServer, Reply, Route, Transfer

_NOTEBOOK = "00000000-0000-4000-8000-000000000200"
_SOURCE = "00000000-0000-4000-8000-000000000201"
_STAGE = Route("POST", "www.googleapis.com", "/upload/drive/v3/files")
_DELETE_BASE = Route("DELETE", "www.googleapis.com", "/drive/v3/files/baseline-stage")
_DELETE = Route("DELETE", "www.googleapis.com", "/drive/v3/files/fault-stage")
_PAYLOAD = b"name,value\nlocal,1\n"
# Independent expected multipart fixture from the documented Drive upload wire shape.
_BODY = (
    b"--notebooklm-android-staging\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
    b'{"name": "data.csv", "mimeType": "text/csv"}\r\n--notebooklm-android-staging\r\n'
    b"Content-Type: text/csv\r\n\r\n" + _PAYLOAD + b"\r\n--notebooklm-android-staging--"
)
_VARIANTS = (
    "success",
    "registration_refusal",
    "registration_ambiguous",
    "import_ambiguous",
    "import_timeout",
    "terminal_failure",
    "cleanup_failed_success",
    "cleanup_failed_refusal",
    "cancel_during_stage",
    "cancel_after_stage",
    "cancel_during_import",
    "close_during_import",
    "deadline_before_cleanup",
)
SCENARIOS = tuple(sorted(f"drive_{variant}" for variant in _VARIANTS))


def _source(*, title: str = "data.csv", status: int = settings.SOURCE_STATUS_COMPLETE) -> Any:
    return read_pb2.Source(
        source_id=read_pb2.SourceId(id=_SOURCE),
        title=title,
        metadata=read_pb2.SourceMetadata(
            original_source_content_type=read_pb2.SOURCE_CONTENT_TYPE_PDF
        ),
        settings=settings.SourceSettings(status=status),
    )


def _registration(request: Any) -> Any:
    return sources_pb2.AddTentativeSourcesResponse(
        tentative_sources=[
            _source(
                title=request.tentative_sources_metadata[0].name,
                status=settings.SOURCE_STATUS_TENTATIVE,
            )
        ]
    )


def _project(*, status: int = settings.SOURCE_STATUS_COMPLETE, present: bool = True) -> Any:
    return read_pb2.GetProjectResponse(
        project=read_pb2.Project(
            id=_NOTEBOOK,
            title="Drive notebook",
            sources=[_source(status=status)] if present else [],
        )
    )


def _stage(stage_id: str, *, held: bool = False) -> Transfer:
    return Transfer(
        response=Reply(
            body=f'{{"id":"{stage_id}"}}'.encode(), headers={"Content-Type": "application/json"}
        ),
        expected_size=len(_BODY),
        expected_digest=hashlib.sha256(_BODY).hexdigest(),
        commit_id=stage_id,
        gates={"commit": "stage-held"} if held else {},
    )


async def run_scenario(
    name: str, *, operation_id: str, result: ScenarioResult | None = None
) -> ScenarioResult:
    if name not in SCENARIOS:
        raise ValueError("unknown Drive fault case")
    result = result or ScenarioResult("android", name, operation_id)
    variant = name.removeprefix("drive_")
    result.record(
        "plan",
        faults=[variant],
        cohort_ids=[operation_id],
        transport="grpc+httpx",
        public_entry="sources.add_file(csv)",
        wait_timeout=0.8 if variant == "import_timeout" else 2.0,
        budgets={
            "baseline_wait_timeout_s": 2.0,
            "rpc_timeout_s": 2.0,
            "upload_http_timeout_s": 1.0,
            "drive_cleanup_deadline_s": 300.0,
            "fault_wait_timeout_s": 0.8 if variant == "import_timeout" else 2.0,
            "cleanup_timeout_s": 2.0,
        },
        clock_evidence="injected Drive cleanup fence only"
        if variant == "deadline_before_cleanup"
        else "real monotonic",
        required_checks=(
            (
                ["staging_identity_retained"]
                if variant
                not in {
                    "success",
                    "cleanup_failed_success",
                    "deadline_before_cleanup",
                    "cancel_during_stage",
                }
                else []
            )
            + (["cleanup_failure_observed"] if variant.startswith("cleanup_failed") else [])
            + [
                "fixture_decoded",
                "expected_outcome",
                "cleanup_policy",
                "no_replay",
                "resources_released",
                "recovered",
                "scripts_consumed",
                "cleanup",
            ]
        ),
        invariants=["I1", "I2", "I4", "I6", "I7", "I8"],
    )
    server = HttpFaultServer(hosts=[_STAGE.host])
    rpc = GrpcFaultServer()
    server.enqueue(
        _STAGE,
        _stage("baseline-stage"),
        _stage("fault-stage", held=variant == "cancel_during_stage"),
    )
    server.enqueue(_DELETE_BASE, Reply(204))
    rpc_actions = {
        ADD_TENTATIVE_SOURCES: [reply(_registration)],
        ADD_SOURCES: [reply(sources_pb2.AddSourcesResponse(sources=[_source()]))],
        GET_PROJECT: [reply(_project()), reply(_project())],
    }

    def append(method: str, *actions: Any) -> None:
        rpc_actions[method].extend(actions)

    permits_cleanup = variant in {
        "success",
        "registration_refusal",
        "terminal_failure",
        "cleanup_failed_success",
        "cleanup_failed_refusal",
    }
    if permits_cleanup:
        server.enqueue(_DELETE, Reply(503 if variant.startswith("cleanup_failed") else 204))
    if variant in {"registration_refusal", "cleanup_failed_refusal"}:
        append(ADD_TENTATIVE_SOURCES, abort(grpc.StatusCode.UNAUTHENTICATED))
    elif variant == "registration_ambiguous":
        append(ADD_TENTATIVE_SOURCES, abort(grpc.StatusCode.UNAVAILABLE))
    elif variant == "cancel_after_stage":
        append(ADD_TENTATIVE_SOURCES, wait_reply("register-held", _registration))
    elif variant != "cancel_during_stage":
        append(ADD_TENTATIVE_SOURCES, reply(_registration))
        if variant == "import_ambiguous":
            append(ADD_SOURCES, commit_abort(grpc.StatusCode.UNAVAILABLE))
            append(GET_PROJECT, reply(_project(present=False)))
        elif variant in {"cancel_during_import", "close_during_import"}:
            append(
                ADD_SOURCES,
                wait_reply("import-held", sources_pb2.AddSourcesResponse(sources=[_source()])),
            )
        else:
            append(ADD_SOURCES, reply(sources_pb2.AddSourcesResponse(sources=[_source()])))
            append(GET_PROJECT, reply(_project()))
            if variant in {"import_timeout", "deadline_before_cleanup"}:
                append(GET_PROJECT, wait_reply("poll-held", _project()))
            else:
                append(
                    GET_PROJECT,
                    reply(
                        _project(
                            status=settings.SOURCE_STATUS_ERROR
                            if variant == "terminal_failure"
                            else settings.SOURCE_STATUS_COMPLETE
                        )
                    ),
                )
    append(GET_PROJECT, reply(_project()))
    for method, actions in rpc_actions.items():
        rpc.plan(method, *actions)
    harness = None
    owned: list[asyncio.Task[Any]] = []

    def release() -> None:
        server.release("stage-held")
        for gate in ("register-held", "import-held", "poll-held"):
            rpc.gate(gate).set()

    try:
        async with android_cohort(
            result,
            rpc,
            server,
            lambda: build_android_client(
                rpc, http_server=server, upload_timeout=1.0, timeout=2.0, server_error_max_retries=0
            ),
            release=release,
            tasks=owned,
        ) as harness:
            client = harness.client
            drive_clock = [time.monotonic()]
            if variant == "deadline_before_cleanup":
                # The production Drive cleanup lifetime has a 300s floor. This
                # selected fence arithmetic uses an instance clock; other cases
                # retain real clocks for operation/transport timeout evidence.
                client._android_runtime.upload_pipeline._monotonic = lambda: drive_clock[0]
                result.record("clock", kind_label="injected Drive cleanup fence only")
            with tempfile.TemporaryDirectory(prefix="fault-drive-") as directory:
                path = Path(directory) / "data.csv"
                path.write_bytes(_PAYLOAD)
                baseline = await client.sources.add_file(_NOTEBOOK, path, wait_timeout=2.0)
                result.require("fixture_decoded", baseline.id == _SOURCE)
                task = asyncio.create_task(
                    client.sources.add_file(
                        _NOTEBOOK,
                        path,
                        wait_timeout=0.8 if variant == "import_timeout" else 2.0,
                    )
                )
                owned.append(task)
                if variant == "cancel_during_stage":
                    await server.wait_for_gate("stage-held")
                    task.cancel()
                elif variant in {
                    "cancel_after_stage",
                    "cancel_during_import",
                    "close_during_import",
                }:
                    method = (
                        ADD_TENTATIVE_SOURCES if variant == "cancel_after_stage" else ADD_SOURCES
                    )
                    await rpc.wait_for_requests(method, 2)
                    if variant == "close_during_import":
                        await asyncio.wait_for(client.close(drain=False), 2)
                    else:
                        task.cancel()
                if variant == "deadline_before_cleanup":
                    await rpc.wait_for_requests(GET_PROJECT, 4)
                    drive_clock[0] += 301.0
                    rpc.gate("poll-held").set()
                error: BaseException | None = None
                value = None
                try:
                    value = await asyncio.wait_for(task, 4)
                except (Exception, asyncio.CancelledError) as exc:
                    error = exc
                metadata = getattr(error, "operation_metadata", None) or getattr(
                    error, "_operation_metadata", None
                )
                prerequisites = getattr(metadata, "prerequisite_ids", ())
                result.record(
                    "outcome",
                    error=None if error is None else type(error).__name__,
                    commit_state=getattr(getattr(error, "commit_state", None), "value", None),
                    prerequisite_retained="fault-stage" in prerequisites,
                )
                if variant in {
                    "success",
                    "cleanup_failed_success",
                    "deadline_before_cleanup",
                }:
                    expected = error is None and value.id == _SOURCE
                elif variant in {"registration_refusal", "cleanup_failed_refusal"}:
                    expected = isinstance(error, AuthError)
                elif variant in {"registration_ambiguous", "import_ambiguous"}:
                    expected = isinstance(error, SourceAddError)
                elif variant == "terminal_failure":
                    expected = isinstance(error, SourceProcessingError)
                elif variant.startswith("cancel_"):
                    expected = isinstance(error, asyncio.CancelledError)
                elif variant == "close_during_import":
                    expected = isinstance(error, (RuntimeError, asyncio.CancelledError))
                else:
                    expected = isinstance(error, RPCTimeoutError)
                result.require("expected_outcome", expected)
                if error is not None and variant != "cancel_during_stage":
                    result.require("staging_identity_retained", "fault-stage" in prerequisites)

                server.release("stage-held")
                for gate in ("register-held", "import-held", "poll-held"):
                    rpc.gate(gate).set()
                if variant == "close_during_import":
                    await client.__aenter__()
                recovered = await client.notebooks.get(_NOTEBOOK)
                result.require("recovered", recovered.id == _NOTEBOOK)
                deletes = [r for r in server.journal if r.route == _DELETE]
                result.require("cleanup_policy", len(deletes) == int(permits_cleanup))
                if variant.startswith("cleanup_failed"):
                    result.require("cleanup_failure_observed", deletes[0].response_status == 503)
                result.require(
                    "no_replay",
                    len([r for r in server.journal if r.route == _STAGE]) == 2
                    and len([r for r in rpc.requests if r.method == ADD_SOURCES]) <= 2,
                )
                result.require(
                    "resources_released",
                    not client._android_runtime.upload_pipeline._transport_clients,
                )
            await rpc.wait_for_idle()
            rpc.assert_consumed()
            server.assert_drained()
            result.require("scripts_consumed", True)
    finally:
        result.record(
            "http_trace",
            requests=[
                {
                    "method": r.route.method,
                    "bytes": r.body_bytes,
                    "digest": r.body_digest,
                    "status": r.response_status,
                }
                for r in server.journal
            ],
            commits=server.committed,
        )
    return result
