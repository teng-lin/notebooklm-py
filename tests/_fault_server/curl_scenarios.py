"""Explicit optional curl lane; all HTTP uses real curl handles and local TLS."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

from notebooklm import NetworkError, ServerError
from notebooklm.exceptions import ArtifactDownloadError

from .common import ScenarioResult
from .curl_routing import CurlFaultServer, CurlRouting, build_curl_client
from .http import Disconnect, Reply, Stall, Transfer, Truncate
from .web import list_response, rpc_response
from .web_transfers import (
    ASSET,
    BASE_FINAL,
    FINAL,
    LIST_ASSETS,
    MEDIA,
    NOTEBOOK,
    READ,
    REGISTER,
    SOURCE,
    UPLOAD,
    _audio_rows,
    _finalize,
    _registration,
    _start,
)


@asynccontextmanager
async def _cohort(result: ScenarioResult, server: CurlFaultServer) -> AsyncIterator[Any]:
    from .web_scenarios import _record_http_trace

    routing = None
    client = None
    primary_error: BaseException | None = None
    cleanup_errors: dict[str, BaseException] = {}
    try:
        await server.__aenter__()
        routing = CurlRouting(server)
        client = build_curl_client(routing)
        await client.__aenter__()
        yield client
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        for label, resource in (("client", client), ("routing", routing), ("server", server)):
            if resource is None:
                continue
            try:
                close = resource.close(drain=False) if label == "client" else resource.aclose()
                await asyncio.wait_for(close, 4)
            except BaseException as exc:
                cleanup_errors[label] = exc
        _record_http_trace(result, server)
        result.record(
            "transport",
            selected="curl_cffi",
            sessions=0 if routing is None else len(routing.sessions),
            upload_handles=0 if routing is None else len(routing.handles),
            body_descriptors=0 if routing is None else len(routing.body_descriptors),
            tls_peer_verified=True,
            tls_hostname_verified=True,
        )
        result.record("transfer_trace", phases=server.events, commits=server.committed)
        result.record(
            "cleanup",
            primary_error=None if primary_error is None else type(primary_error).__name__,
            errors={label: type(error).__name__ for label, error in cleanup_errors.items()},
            client_closed=client is None or not client._lifecycle.is_open(),
            routing_closed=routing is None or routing._closed,
            active_handlers=server.active_handlers,
        )
        for error in cleanup_errors.values():
            if primary_error is None or (
                isinstance(primary_error, Exception) and not isinstance(error, Exception)
            ):
                raise error
        if primary_error is None:
            result.require(
                "real_curl_session_used", routing is not None and len(routing.sessions) > 0
            )
            result.require("curl_resources_settled", routing is not None and routing._closed)
            server.assert_drained()
            result.require("server_requests_expected", True)
            result.require("server_handlers_settled", server.active_handlers == 0)


def _read_reply() -> Reply:
    return Reply(body=list_response(READ.rpc_id or "", [("curl-ready", "Curl")]))


async def _recover(result: ScenarioResult, client: Any) -> None:
    result.require(
        "same_client_recovery", [row.id for row in await client.notebooks.list()] == ["curl-ready"]
    )


async def read_case(result: ScenarioResult) -> None:
    server = CurlFaultServer()
    server.enqueue(READ, _read_reply(), Reply(503), _read_reply())
    async with _cohort(result, server) as client:
        baseline = await client.notebooks.list()
        result.require("successful_read_baseline", [row.id for row in baseline] == ["curl-ready"])
        error = None
        try:
            await client.notebooks.list()
        except ServerError as exc:
            error = exc
        result.require("read_failure_typed", isinstance(error, ServerError))
        await _recover(result, client)
    result.require("read_dispatch_count", len(server.journal) == 3)


async def upload_case(result: ScenarioResult, variant: str) -> None:
    server = CurlFaultServer()
    baseline = b"curl baseline\n"
    body = b"curl transfer\n" * 16000
    server.enqueue(REGISTER, _registration("curl-baseline"), _registration())
    server.enqueue(UPLOAD, _start(baseline=True), _start())
    server.enqueue(BASE_FINAL, _finalize(baseline, "curl-baseline"))
    if variant == "success":
        action = _finalize(body, "curl-upload")
    elif variant == "prefix_failure":
        action = Transfer(prefix_bytes=4096, disconnect_at="body_prefix")
    elif variant == "commit_loss":
        action = _finalize(body, "curl-upload", Disconnect())
    elif variant == "cancel":
        finalized = _finalize(body, "curl-upload")
        action = Transfer(
            prefix_bytes=4096,
            gates={"body_prefix": "curl-upload-body"},
            expected_size=finalized.expected_size,
            expected_digest=finalized.expected_digest,
            commit_id=finalized.commit_id,
        )
    else:
        raise ValueError(variant)
    server.enqueue(FINAL, action)
    server.enqueue(READ, _read_reply())
    with tempfile.TemporaryDirectory(prefix="fault-curl-upload-") as directory:
        source = Path(directory) / "source.txt"
        async with _cohort(result, server) as client:
            source.write_bytes(baseline)
            uploaded = await client.sources.add_file(NOTEBOOK, source)
            result.require("successful_upload_baseline", uploaded.id == "curl-baseline")
            source.write_bytes(body)
            task = asyncio.create_task(client.sources.add_file(NOTEBOOK, source))
            if variant == "cancel":
                await server.wait_for_gate("curl-upload-body")
                task.cancel()
                await asyncio.sleep(0)
                result.require("curl_upload_waits_for_worker", not task.done())
                server.release("curl-upload-body")
            error = None
            try:
                uploaded = await task
            except BaseException as exc:
                error = exc
            if variant == "success":
                result.require("uploaded_identity", error is None and uploaded.id == SOURCE)
            elif variant == "cancel":
                result.require(
                    "upload_cancel_propagated", isinstance(error, asyncio.CancelledError)
                )
            else:
                result.require("upload_failure_typed", isinstance(error, NetworkError))
                result.require(
                    "upload_identity_retained", getattr(error, "source_id", None) == SOURCE
                )
                result.require(
                    "upload_stage_retained", getattr(error, "stage", None) == "upload_finalize"
                )
            result.record("outcome", error=None if error is None else type(error).__name__)
            uploader = client._web_runtime.source_uploader
            result.require("curl_upload_children_settled", not uploader._transport_tasks)
            result.require("curl_upload_clients_settled", not uploader._transport_clients)
            await _recover(result, client)
    result.require(
        "one_finalize_per_session", len([r for r in server.journal if r.route == FINAL]) == 1
    )
    result.require(
        "curl_independent_commit",
        server.committed
        == (["curl-baseline"] if variant == "prefix_failure" else ["curl-baseline", "curl-upload"]),
    )
    if variant == "prefix_failure":
        failed = next(r for r in server.journal if r.route == FINAL)
        result.require("curl_actual_partial_request", 0 < failed.body_bytes < len(body))


async def download_case(result: ScenarioResult, variant: str) -> None:
    server = CurlFaultServer()
    good = Reply(body=MEDIA, headers={"content-type": "audio/wav"})
    server.enqueue(
        LIST_ASSETS,
        *[Reply(body=rpc_response(LIST_ASSETS.rpc_id or "", [_audio_rows()])) for _ in range(2)],
    )
    server.enqueue(ASSET, good)
    if variant == "success":
        action = good
    elif variant == "prefix_failure":
        action = Truncate(MEDIA[:20], len(MEDIA))
    elif variant in {"body_stall", "cancel", "close_reopen"}:
        action = Stall("body", "curl-download-body", good, prefix=MEDIA[:20])
    else:
        raise ValueError(variant)
    server.enqueue(ASSET, action)
    server.enqueue(READ, _read_reply())
    with tempfile.TemporaryDirectory(prefix="fault-curl-download-") as directory:
        destination = Path(directory) / "audio.wav"
        async with _cohort(result, server) as client:

            async def download() -> Any:
                return await client.artifacts.download_audio(
                    NOTEBOOK, str(destination), artifact_id="audio-fault"
                )

            await download()
            result.require("successful_download_baseline", destination.read_bytes() == MEDIA)
            destination.write_bytes(b"existing")
            task = asyncio.create_task(download())
            if variant in {"cancel", "close_reopen"}:
                await server.wait_for_gate("curl-download-body")
                if variant == "cancel":
                    task.cancel()
                else:
                    await client.close(drain=False)
            error = None
            try:
                await task
            except BaseException as exc:
                error = exc
            if variant == "success":
                result.require(
                    "download_integrity", error is None and destination.read_bytes() == MEDIA
                )
            else:
                expected = (
                    asyncio.CancelledError
                    if variant in {"cancel", "close_reopen"}
                    else ArtifactDownloadError
                )
                result.require("download_failure_typed", isinstance(error, expected))
                result.require("existing_file_preserved", destination.read_bytes() == b"existing")
            result.record("outcome", error=None if error is None else type(error).__name__)
            result.require("curl_staging_removed", list(Path(directory).iterdir()) == [destination])
            if variant == "close_reopen":
                await client.__aenter__()
            await _recover(result, client)
            server.release("curl-download-body")
    result.require(
        "curl_download_not_replayed", len([r for r in server.journal if r.route == ASSET]) == 2
    )


_IMPLEMENTATIONS = {
    "curl_read_recovery": read_case,
    **{
        f"curl_upload_{name}": partial(upload_case, variant=name)
        for name in ("success", "prefix_failure", "commit_loss", "cancel")
    },
    **{
        f"curl_download_{name}": partial(download_case, variant=name)
        for name in ("success", "prefix_failure", "body_stall", "cancel", "close_reopen")
    },
}
SCENARIOS = tuple(sorted(_IMPLEMENTATIONS))


async def run_scenario(
    name: str, *, operation_id: str, result: ScenarioResult | None = None
) -> ScenarioResult:
    if name not in _IMPLEMENTATIONS:
        raise ValueError("unknown curl fault scenario")
    if result is None:
        result = ScenarioResult("web", name, operation_id)
    elif (result.backend, result.scenario, result.operation_id) != ("web", name, operation_id):
        raise ValueError("curl result identity mismatch")
    required = [
        "real_curl_session_used",
        "curl_resources_settled",
        "server_requests_expected",
        "server_handlers_settled",
        "same_client_recovery",
    ]
    if name.startswith("curl_upload_"):
        required += [
            "successful_upload_baseline",
            "curl_upload_children_settled",
            "curl_upload_clients_settled",
            "one_finalize_per_session",
            "curl_independent_commit",
        ]
    elif name.startswith("curl_download_"):
        required += [
            "successful_download_baseline",
            "curl_staging_removed",
            "curl_download_not_replayed",
        ]
    else:
        required += ["successful_read_baseline", "read_failure_typed", "read_dispatch_count"]
    result.record(
        "plan",
        faults=["curl:valid-baseline", name, "same-client:recovery"],
        cohort_ids=[f"{operation_id}:0"],
        transport="curl_cffi",
        rpc_timeout=0.5,
        transfer_timeout=0.5,
        rate_limit_max_retries=0,
        server_error_max_retries=0,
        cleanup_timeout=4,
        required_checks=required,
    )
    await _IMPLEMENTATIONS[name](result)
    return result
