"""Local publication faults through public audio downloads over real sockets."""

from __future__ import annotations

import errno
import hashlib
import tempfile
import threading
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from .common import ScenarioResult
from .http import HttpFaultServer, Reply
from .web import rpc_response
from .web_transfers import ASSET, LIST_ASSETS, MEDIA, _audio_rows, _download

VARIANTS = ("create", "write_prefix", "flush", "replace", "cleanup")
CHECKS = [
    "primary_io_error",
    "destination_preserved",
    "fault_reached",
    "staging_settled",
    "writer_closed",
    "writer_thread_settled",
    "asset_owners_settled",
    "responses_closed",
    "network_body_evidence",
    "local_write_evidence",
    "same_client_download_recovery",
    "no_remote_mutation",
    "bounded_requests",
    "server_required_gates_observed",
    "server_plan_consumed",
    "server_had_no_errors",
    "server_handlers_drained",
]


class _ObservedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, body: bytearray):
        self.stream, self.body = stream, body

    async def __aiter__(self):
        async for chunk in self.stream:
            self.body.extend(chunk)
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class _Writer:
    def __init__(self, handle: Any, variant: str, error: OSError, reached: list[str]):
        self.handle, self.variant, self.error, self.reached = handle, variant, error, reached
        self.written = 0

    def __enter__(self):
        return self

    def write(self, data: bytes):
        if self.variant == "write_prefix":
            self.handle.write(data[:8])
            self.handle.flush()
            self.written = self.handle.tell()
            self.reached.append("write_prefix")
            raise self.error
        written = self.handle.write(data)
        self.written += written
        return written

    def __exit__(self, *args):
        try:
            if self.variant == "flush":
                self.handle.flush()
                self.reached.append("flush")
                raise self.error
        finally:
            self.handle.close()


async def download_persistence_case(
    result: ScenarioResult, *, variant: str, existing: bool = True
) -> None:
    from .web_scenarios import _cohort, _requests, _require_clean

    server = HttpFaultServer(hosts=["lh3.googleusercontent.com"])
    server.enqueue(
        LIST_ASSETS,
        *[Reply(body=rpc_response(LIST_ASSETS.rpc_id or "", [_audio_rows()])) for _ in range(2)],
    )
    asset_attempts = 1 if variant == "create" else 2
    server.enqueue(
        ASSET,
        *[Reply(body=MEDIA, headers={"content-type": "audio/wav"}) for _ in range(asset_attempts)],
    )
    responses: list[httpx.Response] = []
    bodies: list[bytearray] = []
    handles: list[Any] = []
    writers: list[_Writer] = []
    reached: list[str] = []
    failure = OSError(
        errno.ENOSPC if variant in {"create", "write_prefix"} else errno.EIO,
        "injected local storage failure",
    )
    if variant in {"replace", "cleanup"}:
        failure = PermissionError(errno.EACCES, "injected publication failure")

    async def observe(response: httpx.Response):
        responses.append(response)
        body = bytearray()
        bodies.append(body)
        response.stream = _ObservedStream(response.stream, body)

    def factory(**kwargs):
        kwargs["event_hooks"] = {"response": [observe]}
        return server.client_factory(**kwargs)

    with tempfile.TemporaryDirectory(prefix="fault-publication-") as directory:
        destination = Path(directory) / f"{Path(directory).name}.wav"
        if existing:
            destination.write_bytes(b"previous asset")
        async with _cohort(
            result, server, transfer_timeout=1, transfer_client_factory=factory, operation_timeout=5
        ) as client:
            owner = client.artifacts._asset_downloads
            create, publish, remove, open_file = (
                owner._create_download_staging,
                owner._publish_download,
                owner._remove_download_staging,
                owner._open_download_staging,
            )

            def fail_create(path):
                reached.append("create")
                raise failure

            def fail_publish(staging, target):
                reached.append("replace")
                raise failure

            def fail_remove(staging):
                reached.append("cleanup")
                raise OSError(errno.EIO, "injected secondary cleanup failure")

            def observed_open(path):
                handle = open_file(path)
                handles.append(handle)
                writer = _Writer(handle, variant, failure, reached)
                writers.append(writer)
                return writer

            owner._open_download_staging = observed_open
            if variant == "create":
                owner._create_download_staging = fail_create
            if variant in {"replace", "cleanup"}:
                owner._publish_download = fail_publish
            if variant == "cleanup":
                owner._remove_download_staging = fail_remove
            error = None
            try:
                await _download(client, destination, batch=False)
            except OSError as exc:
                error = exc
            finally:
                owner._create_download_staging = create
                owner._publish_download = publish
                owner._remove_download_staging = remove
                owner._open_download_staging = open_file
            result.require("primary_io_error", error is failure)
            result.require(
                "destination_preserved",
                destination.read_bytes() == b"previous asset"
                if existing
                else not destination.exists(),
            )
            result.require(
                "fault_reached", ("replace" if variant == "cleanup" else variant) in reached
            )
            staging = list(Path(directory).glob("*.tmp"))
            result.require("staging_settled", len(staging) == (1 if variant == "cleanup" else 0))
            result.record(
                "local_storage",
                failure=type(error).__name__,
                stages=reached,
                retained=len(staging),
                existing=existing,
            )
            result.require(
                "local_write_evidence",
                [writer.written for writer in writers]
                == (
                    [] if variant == "create" else [8 if variant == "write_prefix" else len(MEDIA)]
                ),
            )
            result.require("writer_closed", all(handle.closed for handle in handles))
            result.require(
                "writer_thread_settled",
                not any(
                    t.name.startswith(f"artifact-dl-writer-{destination.name}.")
                    for t in threading.enumerate()
                ),
            )
            result.require("asset_owners_settled", not owner._clients and not owner._tasks)
            result.require("responses_closed", all(response.is_closed for response in responses))
            # Observe transport-delivered bytes separately from the disk writer.
            result.require(
                "network_body_evidence",
                not bodies
                if variant == "create"
                else len(bodies) == 1 and bytes(bodies[0]) == MEDIA,
            )
            result.record(
                "received_body",
                bytes=sum(map(len, bodies)),
                sha256=[hashlib.sha256(body).hexdigest() for body in bodies],
            )
            await _download(client, destination, batch=False)
            result.require("same_client_download_recovery", destination.read_bytes() == MEDIA)
            for path in staging:
                path.unlink()
        result.require("no_remote_mutation", not server.committed)
        result.require(
            "bounded_requests",
            len(_requests(server, ASSET)) == asset_attempts
            and len(_requests(server, LIST_ASSETS)) == 2,
        )
        _require_clean(result, server)


IMPLEMENTATIONS = {
    **{
        f"download_local_{variant}": partial(download_persistence_case, variant=variant)
        for variant in VARIANTS
    },
    "download_local_replace_absent": partial(
        download_persistence_case, variant="replace", existing=False
    ),
}
PLANS = {
    name: (("audio:list", "asset:valid", "local:" + name, "download:recovery"), 1)
    for name in IMPLEMENTATIONS
}
REQUIRED_CHECKS = dict.fromkeys(IMPLEMENTATIONS, CHECKS)


async def report_cleanup_case(result: ScenarioResult) -> None:
    """Cover the distinct thread-staged neutral publication owner."""
    from .web_scenarios import _cohort, _requests, _require_clean
    from .web_transfers import NOTEBOOK

    markdown = "# Synthetic report\n"
    rows = [["report", "Report", 2, None, 3, None, None, [markdown]]]
    server = HttpFaultServer()
    server.enqueue(
        LIST_ASSETS, *[Reply(body=rpc_response(LIST_ASSETS.rpc_id or "", [rows])) for _ in range(2)]
    )
    failure = PermissionError(errno.EACCES, "injected report publication failure")
    reached: list[str] = []
    with tempfile.TemporaryDirectory(prefix="fault-report-publication-") as directory:
        destination = Path(directory) / "report.md"
        destination.write_bytes(b"previous report")
        async with _cohort(result, server, operation_timeout=5) as client:
            owner = client.artifacts._asset_downloads
            publish, remove = owner._publish_download, owner._remove_download_staging

            def fail_publish(staging, target):
                reached.append("replace")
                raise failure

            def fail_remove(staging):
                reached.append("cleanup")
                raise OSError(errno.EIO, "injected secondary cleanup failure")

            owner._publish_download, owner._remove_download_staging = fail_publish, fail_remove
            error = None
            try:
                await client.artifacts.download_report(
                    NOTEBOOK, str(destination), artifact_id="report"
                )
            except OSError as exc:
                error = exc
            finally:
                owner._publish_download, owner._remove_download_staging = publish, remove
            result.require("primary_io_error", error is failure)
            result.require("destination_preserved", destination.read_bytes() == b"previous report")
            result.require("fault_reached", reached == ["replace", "cleanup"])
            staging = list(Path(directory).glob("*.tmp"))
            result.require("staging_retention_explicit", len(staging) == 1)
            result.require(
                "writer_settled", staging[0].read_text() == markdown and not owner._tasks
            )
            result.record(
                "local_storage",
                retained=1,
                failure=type(error).__name__,
                staging_sha256=hashlib.sha256(staging[0].read_bytes()).hexdigest(),
            )
            await client.artifacts.download_report(NOTEBOOK, str(destination), artifact_id="report")
            result.require("same_client_download_recovery", destination.read_text() == markdown)
            staging[0].unlink()
        result.require("bounded_requests", len(_requests(server, LIST_ASSETS)) == 2)
        result.require("no_remote_mutation", not server.committed)
        _require_clean(result, server)


IMPLEMENTATIONS["download_local_report_cleanup"] = report_cleanup_case
PLANS["download_local_report_cleanup"] = (
    ("report:list", "replace+cleanup:failure", "report:recovery"),
    1,
)
REQUIRED_CHECKS["download_local_report_cleanup"] = [
    "primary_io_error",
    "destination_preserved",
    "fault_reached",
    "staging_retention_explicit",
    "writer_settled",
    "same_client_download_recovery",
    "bounded_requests",
    "no_remote_mutation",
    "server_required_gates_observed",
    "server_plan_consumed",
    "server_had_no_errors",
    "server_handlers_drained",
]
