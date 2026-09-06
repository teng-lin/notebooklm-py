"""Production Android HTTP uploads with independent socket/commit evidence.

Registration fixture provenance: tests/unit/android/test_source_upload.py::_graph.
The real protobuf decoder and production start/finalize owners remain in use.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path
from typing import Any

import grpc

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
    sources_pb2,
)
from notebooklm._android.sources import ADD_TENTATIVE_SOURCES_METHOD
from notebooklm.exceptions import AuthError, SourceAddError
from notebooklm.outcomes import CommitState

from .android import build_android_client
from .android_cleanup import android_cohort
from .common import ScenarioResult
from .grpc import GET_PROJECT, GrpcFaultServer, reply, wait_abort
from .http import Disconnect, HttpFaultServer, Reply, Route, Stall, Transfer

_NOTEBOOK = "00000000-0000-4000-8000-000000000200"
_SOURCE = "00000000-0000-4000-8000-000000000201"
_HOST = "notebooklm-pa.googleapis.com"
_PATH = f"/upload/upload/{_NOTEBOOK}"
_START = Route("POST", _HOST, _PATH)
_FINAL = Route("PUT", _HOST, _PATH, upload_id="fault-session")
_BASE_FINAL = Route("PUT", _HOST, _PATH, upload_id="baseline-session")
_SESSION = (
    f"https://{_HOST}{_PATH}?upload_id=synthetic-private-capability&upload_protocol=resumable"
)
SCENARIOS = tuple(
    sorted(
        f"upload_{variant}"
        for variant in (
            "success",
            "registration_failure",
            "registration_refusal",
            "repeated_cancel",
            "start_failure",
            "prefix_disconnect",
            "body_stall",
            "commit_loss",
            "start_401",
            "start_403",
            "finalize_401",
            "finalize_403",
            "cancel_before_finalize",
            "cancel_after_prefix",
            "close_reopen",
        )
    )
)


def _registration() -> Any:
    return sources_pb2.AddTentativeSourcesResponse(
        tentative_sources=[
            read_pb2.Source(source_id=read_pb2.SourceId(id=_SOURCE), title="payload.pdf")
        ]
    )


def _start_reply(session_id: str = "fault-session") -> Reply:
    return Reply(
        headers={
            "X-Goog-Upload-URL": _SESSION.replace("synthetic-private-capability", session_id),
            "X-Goog-Upload-Status": "active",
        }
    )


def _final_reply(payload: bytes, commit_id: str) -> Transfer:
    return Transfer(
        require_session=True,
        response=Reply(headers={"X-Goog-Upload-Status": "final"}),
        expected_size=len(payload),
        expected_digest=hashlib.sha256(payload).hexdigest(),
        commit_id=commit_id,
    )


def _trace(result: ScenarioResult, server: HttpFaultServer, rpc: GrpcFaultServer) -> None:
    result.record(
        "http_trace",
        events=server.events,
        requests=[
            {
                "method": item.route.method,
                "logical_route": "android_upload",
                "bytes": item.body_bytes,
                "digest": item.body_digest,
                "complete": item.body_complete,
                "bearer_attached": item.headers.get("authorization", "").startswith(
                    "Bearer fault-bearer-"
                ),
            }
            for item in server.journal
        ],
        committed=server.committed,
    )
    result.record("grpc_journal", methods=[r.method.rpartition("/")[2] for r in rpc.requests])


class _UploadResources:
    """Retain actual native upload owners through the public progress callback."""

    def __init__(self) -> None:
        self.files: set[Any] = set()
        self.clients: set[Any] = set()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.permits: dict[asyncio.Semaphore, int] = {}
        self.permit_observed_held = False

    def capture(self, pipeline: Any) -> None:
        self.files.update(pipeline._open_files)
        self.clients.update(pipeline._transport_clients)
        self.tasks.update(pipeline._transport_tasks)
        semaphore = pipeline._upload_semaphore
        if semaphore is not None:
            self.permits[semaphore] = pipeline._max_concurrent_uploads
            self.permit_observed_held |= semaphore._value < pipeline._max_concurrent_uploads

    def require_settled(self, result: ScenarioResult, pipeline: Any) -> None:
        result.record(
            "native_resources",
            observed_files=len(self.files),
            observed_tasks=len(self.tasks),
            observed_clients=len(self.clients),
            open_files=sum(not item.closed for item in self.files),
            active_tasks=sum(not item.done() for item in self.tasks),
            held_permits=sum(limit - item._value for item, limit in self.permits.items()),
        )
        result.require(
            "native_descriptors_closed",
            len(self.files) == 2
            and all(item.closed for item in self.files)
            and not pipeline._open_files,
        )
        result.require(
            "native_tasks_settled",
            bool(self.tasks)
            and all(item.done() for item in self.tasks)
            and not pipeline._transport_tasks,
        )
        result.require(
            "native_clients_closed",
            bool(self.clients)
            and all(item.is_closed for item in self.clients)
            and not pipeline._transport_clients,
        )
        result.require(
            "native_permits_released",
            self.permit_observed_held
            and all(item._value == limit for item, limit in self.permits.items()),
        )


async def run_scenario(
    name: str,
    *,
    operation_id: str,
    result: ScenarioResult | None = None,
) -> ScenarioResult:
    if name not in SCENARIOS:
        raise ValueError("unknown Android upload scenario")
    result = result or ScenarioResult("android", name, operation_id)
    if (result.backend, result.scenario, result.operation_id) != ("android", name, operation_id):
        raise ValueError("inconsistent Android transfer identity")
    variant = name.removeprefix("upload_")
    required = [
        "fixture_decoded",
        "expected_outcome",
        "exact_phase_counts",
        "commit_count",
        "resources_released",
        "native_descriptors_closed",
        "native_tasks_settled",
        "native_clients_closed",
        "native_permits_released",
        "recovered",
        "scripts_consumed",
        "cleanup",
    ]
    if variant in {"registration_failure", "registration_refusal"}:
        required.extend(["registration_never_claims_source", "registration_commit_evidence"])
    elif variant not in {
        "success",
        "cancel_before_finalize",
        "cancel_after_prefix",
        "close_reopen",
        "repeated_cancel",
    }:
        required.extend(["registered_identity", "stage_identity"])
    if variant == "repeated_cancel":
        required.append("repeated_cancel_during_writer_settlement")
    result.record(
        "plan",
        faults=[variant],
        cohort_ids=[operation_id],
        transport="httpx",
        public_entry="sources.add_file",
        upload_timeout=1.0,
        budgets={
            "rpc_timeout_s": 2.0,
            "upload_http_timeout_s": 1.0,
            "upload_aggregate_timeout_s": 300.0,
            "operation_watchdog_s": 4,
            "cleanup_timeout_s": 2,
        },
        required_checks=required,
        invariants=["I1", "I2", "I3", "I4", "I7", "I8"],
    )
    # Upload is opaque PDF transfer; source fixture title/identity is decoded by production.
    payload = b"%PDF-1.4\n% synthetic upload body\n" + b"x" * 131072 + b"\n%%EOF\n"
    fault_payload = payload
    if variant == "body_stall":
        fault_payload = payload + b"x" * (8 * 1024 * 1024)
    server = HttpFaultServer(hosts=[_HOST], max_body=16 * 1024 * 1024)
    rpc = GrpcFaultServer()
    observed = _UploadResources()
    harness = None

    def registration(_request: Any) -> Any:
        observed.capture(harness.client._android_runtime.upload_pipeline)
        return _registration()

    registration_failure = variant in {"registration_failure", "registration_refusal"}
    registration_action = (
        wait_abort(
            "registration-held",
            grpc.StatusCode.UNAUTHENTICATED
            if variant == "registration_refusal"
            else grpc.StatusCode.UNAVAILABLE,
        )
        if registration_failure
        else reply(registration)
    )
    rpc.plan(ADD_TENTATIVE_SOURCES_METHOD, reply(registration), registration_action)
    rpc.plan(GET_PROJECT, reply())
    server.enqueue(_START, _start_reply("baseline-session"))
    server.enqueue(_BASE_FINAL, _final_reply(payload, "baseline"))
    if registration_failure:
        pass
    elif variant == "start_failure":
        server.enqueue(_START, Reply(503))
    elif variant in {"start_401", "start_403"}:
        server.enqueue(_START, Reply(int(variant[-3:])))
    elif variant == "cancel_before_finalize":
        server.enqueue(_START, Stall("headers", "start-held", _start_reply()))
    else:
        server.enqueue(_START, _start_reply())
        if variant == "prefix_disconnect":
            action = Transfer(require_session=True, prefix_bytes=1024, disconnect_at="body_prefix")
        elif variant in {"body_stall", "cancel_after_prefix", "close_reopen", "repeated_cancel"}:
            action = Transfer(
                require_session=True,
                prefix_bytes=1024,
                gates={"body_prefix": "body-held"},
                allow_abandoned_body=True,
            )
        elif variant == "commit_loss":
            action = Transfer(
                require_session=True,
                response=Disconnect(),
                expected_size=len(payload),
                expected_digest=hashlib.sha256(payload).hexdigest(),
                commit_id="fault",
            )
        elif variant in {"finalize_401", "finalize_403"}:
            action = Transfer(require_session=True, response=Reply(int(variant[-3:])))
        else:
            action = _final_reply(payload, "fault")
        server.enqueue(_FINAL, action)

    owned: list[asyncio.Task[Any]] = []
    progress_entered = asyncio.Event()
    progress_cleanup = asyncio.Event()
    progress_release = asyncio.Event()
    cleanup_release = asyncio.Event()
    fault_phase = False

    def release() -> None:
        server.release("body-held")
        server.release("start-held")
        rpc.gate("registration-held").set()
        progress_release.set()
        cleanup_release.set()

    def transfer_client(**kwargs: Any) -> Any:
        observed.capture(harness.client._android_runtime.upload_pipeline)
        client = server.client_factory(**kwargs)
        observed.clients.add(client)
        return client

    async def progress(_sent: int, _total: int) -> None:
        observed.capture(harness.client._android_runtime.upload_pipeline)
        if fault_phase and variant == "repeated_cancel":
            progress_entered.set()
            try:
                await progress_release.wait()
            finally:
                progress_cleanup.set()
                await cleanup_release.wait()

    try:
        async with android_cohort(
            result,
            rpc,
            server,
            lambda: build_android_client(
                rpc,
                http_server=server,
                upload_timeout=1.0,
                timeout=2,
                http_client_factory=transfer_client,
            ),
            release=release,
            tasks=owned,
        ) as harness:
            client = harness.client
            pipeline = client._android_runtime.upload_pipeline
            with tempfile.TemporaryDirectory(prefix="fault-upload-") as directory:
                path = Path(directory) / "payload.pdf"
                path.write_bytes(payload)
                baseline = await client.sources.add_file(_NOTEBOOK, path, on_progress=progress)
                result.require("fixture_decoded", baseline.id == _SOURCE)
                path.write_bytes(fault_payload)
                fault_phase = True
                task = asyncio.create_task(
                    client.sources.add_file(_NOTEBOOK, path, on_progress=progress)
                )
                owned.append(task)
                if registration_failure:
                    await rpc.wait_for_requests(ADD_TENTATIVE_SOURCES_METHOD, 2)
                    observed.capture(pipeline)
                    rpc.gate("registration-held").set()
                elif variant == "cancel_before_finalize":
                    await server.wait_for_gate("start-held")
                    observed.capture(pipeline)
                    task.cancel()
                elif variant in {"cancel_after_prefix", "close_reopen", "repeated_cancel"}:
                    await server.wait_for_event("body_prefix", count=2)
                    observed.capture(pipeline)
                    if variant == "close_reopen":
                        await asyncio.wait_for(client.close(drain=False), 2)
                    else:
                        if variant == "repeated_cancel":
                            await asyncio.wait_for(progress_entered.wait(), 1)
                        task.cancel()
                        if variant == "repeated_cancel":
                            await asyncio.wait_for(progress_cleanup.wait(), 1)
                            task.cancel()
                            result.require(
                                "repeated_cancel_during_writer_settlement",
                                task.cancelling() == 2 and progress_cleanup.is_set(),
                            )
                            cleanup_release.set()
                error: BaseException | None = None
                value = None
                try:
                    value = await asyncio.wait_for(task, 4)
                except (Exception, asyncio.CancelledError) as exc:
                    error = exc
                result.record(
                    "outcome",
                    error=None if error is None else type(error).__name__,
                    stage=getattr(error, "stage", None),
                    registered_identity_retained=getattr(error, "source_id", None) == _SOURCE,
                )
                if variant == "success":
                    expected = error is None and value.id == _SOURCE
                elif variant.startswith("cancel_") or variant == "repeated_cancel":
                    expected = isinstance(error, asyncio.CancelledError)
                elif variant == "close_reopen":
                    expected = isinstance(error, RuntimeError)
                elif variant.endswith("401") or variant == "registration_refusal":
                    expected = isinstance(error, AuthError)
                else:
                    expected = isinstance(error, SourceAddError)
                result.require("expected_outcome", expected)
                if registration_failure:
                    result.require(
                        "registration_never_claims_source",
                        getattr(error, "source_id", None) is None,
                    )
                    result.require(
                        "registration_commit_evidence",
                        getattr(error, "commit_state", None)
                        is (
                            CommitState.REJECTED
                            if variant == "registration_refusal"
                            else CommitState.UNKNOWN
                        ),
                    )
                elif variant not in {
                    "success",
                    "cancel_before_finalize",
                    "cancel_after_prefix",
                    "close_reopen",
                    "repeated_cancel",
                }:
                    result.require(
                        "registered_identity", getattr(error, "source_id", None) == _SOURCE
                    )
                    stage = "start" if variant.startswith("start_") else "finalize"
                    result.require("stage_identity", getattr(error, "stage", None) == stage)
                release()
                # Capture retained owners BEFORE reopening can reset tracking sets.
                observed.require_settled(result, pipeline)
                result.require(
                    "resources_released",
                    not pipeline._transport_clients
                    and not pipeline._transport_tasks
                    and not pipeline._open_files,
                )
                if variant == "close_reopen":
                    await client.__aenter__()
                recovered = await client.notebooks.get(_NOTEBOOK)
                result.require("recovered", recovered.id == _NOTEBOOK)
                result.require(
                    "exact_phase_counts",
                    sum(r.method == ADD_TENTATIVE_SOURCES_METHOD for r in rpc.requests) == 2
                    and sum(r.route == _START for r in server.journal)
                    == (1 if registration_failure else 2)
                    and sum(r.route in {_FINAL, _BASE_FINAL} for r in server.journal)
                    == (
                        1
                        if registration_failure
                        or variant.startswith("start_")
                        or variant == "cancel_before_finalize"
                        else 2
                    ),
                )
                result.require(
                    "commit_count",
                    server.committed
                    == (
                        ["baseline", "fault"]
                        if variant in {"success", "commit_loss"}
                        else ["baseline"]
                    ),
                )
            await rpc.wait_for_idle()
            rpc.assert_consumed()
            server.assert_drained()
            result.require("scripts_consumed", True)
    finally:
        _trace(result, server, rpc)
    return result
