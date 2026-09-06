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

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
    sources_pb2,
)
from notebooklm._android.sources import ADD_TENTATIVE_SOURCES_METHOD
from notebooklm.exceptions import AuthError, SourceAddError

from .android import build_android_client
from .common import ScenarioResult
from .grpc import GET_PROJECT, GrpcFaultServer, reply
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
        "recovered",
        "scripts_consumed",
        "cleanup",
    ]
    result.record(
        "plan",
        faults=[variant],
        cohort_ids=[operation_id],
        transport="httpx",
        public_entry="sources.add_file",
        upload_timeout=0.25,
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
    rpc.plan(ADD_TENTATIVE_SOURCES_METHOD, reply(_registration()), reply(_registration()))
    rpc.plan(GET_PROJECT, reply())
    server.enqueue(_START, _start_reply("baseline-session"))
    server.enqueue(_BASE_FINAL, _final_reply(payload, "baseline"))
    if variant == "start_failure":
        server.enqueue(_START, Reply(503))
    elif variant in {"start_401", "start_403"}:
        server.enqueue(_START, Reply(int(variant[-3:])))
    elif variant == "cancel_before_finalize":
        server.enqueue(_START, Stall("headers", "start-held", _start_reply()))
    else:
        server.enqueue(_START, _start_reply())
        if variant == "prefix_disconnect":
            action = Transfer(prefix_bytes=1024, disconnect_at="body_prefix")
        elif variant in {"body_stall", "cancel_after_prefix", "close_reopen"}:
            action = Transfer(
                prefix_bytes=1024, gates={"body_prefix": "body-held"}, allow_abandoned_body=True
            )
        elif variant == "commit_loss":
            action = Transfer(
                response=Disconnect(),
                expected_size=len(payload),
                expected_digest=hashlib.sha256(payload).hexdigest(),
                commit_id="fault",
            )
        elif variant in {"finalize_401", "finalize_403"}:
            action = Transfer(response=Reply(int(variant[-3:])))
        else:
            action = _final_reply(payload, "fault")
        server.enqueue(_FINAL, action)
    harness = None
    task: asyncio.Task[Any] | None = None
    try:
        async with rpc, server:
            harness = build_android_client(rpc, http_server=server, upload_timeout=0.25)
            client = harness.client
            try:
                async with client:
                    with tempfile.TemporaryDirectory(prefix="fault-upload-") as directory:
                        path = Path(directory) / "payload.pdf"
                        path.write_bytes(payload)
                        baseline = await client.sources.add_file(_NOTEBOOK, path)
                        result.require("fixture_decoded", baseline.id == _SOURCE)
                        path.write_bytes(fault_payload)
                        task = asyncio.create_task(client.sources.add_file(_NOTEBOOK, path))
                        if variant == "cancel_before_finalize":
                            await server.wait_for_gate("start-held")
                            task.cancel()
                        elif variant in {"cancel_after_prefix", "close_reopen"}:
                            # The baseline already emitted one prefix event.
                            await server.wait_for_event("body_prefix", count=2)
                            if variant == "close_reopen":
                                await asyncio.wait_for(client.close(drain=False), 2)
                            else:
                                task.cancel()
                        error: BaseException | None = None
                        value = None
                        try:
                            value = await asyncio.wait_for(task, 3)
                        except (Exception, asyncio.CancelledError) as exc:
                            error = exc
                        result.record(
                            "outcome",
                            error=None if error is None else type(error).__name__,
                            stage=getattr(error, "stage", None),
                            registered_identity_retained=getattr(error, "source_id", None)
                            == _SOURCE,
                        )
                        if variant == "success":
                            expected = error is None and value.id == _SOURCE
                        elif variant.startswith("cancel_"):
                            expected = isinstance(error, asyncio.CancelledError)
                        elif variant == "close_reopen":
                            expected = isinstance(error, RuntimeError)
                        elif variant.endswith("401"):
                            expected = isinstance(error, AuthError)
                        else:
                            expected = isinstance(error, SourceAddError)
                        result.require("expected_outcome", expected)
                        if variant not in {
                            "success",
                            "cancel_before_finalize",
                            "cancel_after_prefix",
                            "close_reopen",
                        }:
                            result.require(
                                "registered_identity", getattr(error, "source_id", None) == _SOURCE
                            )
                            stage = "start" if variant.startswith("start_") else "finalize"
                            result.require("stage_identity", getattr(error, "stage", None) == stage)
                        server.release("body-held")
                        server.release("start-held")
                        if variant == "close_reopen":
                            await client.__aenter__()
                        recovered = await client.notebooks.get(_NOTEBOOK)
                        result.require("recovered", recovered.id == _NOTEBOOK)
                        pipeline = client._android_runtime.upload_pipeline
                        result.require("resources_released", not pipeline._transport_clients)
                        result.require(
                            "exact_phase_counts",
                            len(
                                [
                                    r
                                    for r in rpc.requests
                                    if r.method == ADD_TENTATIVE_SOURCES_METHOD
                                ]
                            )
                            == 2
                            and len([r for r in server.journal if r.route == _START]) == 2
                            and len([r for r in server.journal if r.route in {_FINAL, _BASE_FINAL}])
                            == (
                                1
                                if variant.startswith("start_")
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
                server.release("body-held")
                server.release("start-held")
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
                await asyncio.wait_for(client.close(drain=False), 2)
    finally:
        _trace(result, server, rpc)
        result.record(
            "cleanup",
            handlers=server.active_handlers,
            rpc_handlers=len(rpc._active),
            client_closed=harness is None or not harness.client._lifecycle.is_open(),
        )
        result.require(
            "cleanup",
            not server.active_handlers
            and not rpc._active
            and (harness is None or not harness.client._lifecycle.is_open()),
        )
    return result
