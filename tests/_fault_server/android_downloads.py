"""Android public infographic and guarded batch publication over real sockets."""

from __future__ import annotations

import asyncio
import hashlib
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import artifacts_pb2
from notebooklm.exceptions import ArtifactDownloadError, AuthError, OperationTimeoutError

from .android import build_android_client
from .android_cleanup import android_cohort
from .common import ScenarioResult
from .grpc import LIST_ARTIFACTS, GrpcFaultServer, reply
from .http import HttpFaultServer, Reply, Route, Stall, Truncate

_HOST = "lh3.googleusercontent.com"
_ASSET = Route("GET", _HOST, "/fault-image")
_TARGET = Route("GET", "lh4.googleusercontent.com", "/redirect-image")
_BOUNCE = Route("GET", _HOST, "/bounce-image")
_URL = f"https://{_HOST}/fault-image?cap=SENTINEL_ASSET_CAPABILITY"
_TARGET_URL = "https://lh4.googleusercontent.com/redirect-image?cap=SENTINEL_SIGNED_HOP"
_NOTEBOOK = "00000000-0000-4000-8000-000000000200"


def _png() -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
        + chunk(b"IEND", b"")
    )


_PAYLOAD = _png()
_VARIANTS = (
    "success",
    "truncation",
    "prefix_disconnect",
    "body_stall",
    "cancel",
    "close_reopen",
    "401",
    "403",
    "trusted_redirect",
    "untrusted_redirect",
    "redirect_loop",
    "expired_capability",
    "html",
    "wrong_signature",
    "bearer_bounce",
)
SCENARIOS = tuple(
    sorted(
        f"download_{'batch_' if batch else ''}{variant}"
        for batch in (False, True)
        for variant in _VARIANTS
    )
)


def _listing() -> Any:
    # Protobuf shape from tests/_fixtures/android_artifacts.py::_artifact.
    artifact = artifacts_pb2.Artifact(
        artifact_id="image",
        title="Image",
        type=artifacts_pb2.ARTIFACT_TYPE_INFOGRAPHIC,
        status=artifacts_pb2.ARTIFACT_STATUS_READY,
    )
    artifact.infographic.infographics.add(title="Image").image.url = _URL
    return artifacts_pb2.ListArtifactsResponse(artifacts=[artifact])


async def run_scenario(
    name: str, *, operation_id: str, result: ScenarioResult | None = None
) -> ScenarioResult:
    if name not in SCENARIOS:
        raise ValueError("unknown Android download scenario")
    result = result or ScenarioResult("android", name, operation_id)
    if (result.backend, result.scenario, result.operation_id) != ("android", name, operation_id):
        raise ValueError("inconsistent Android download identity")
    batch = name.startswith("download_batch_")
    variant = name.removeprefix("download_batch_" if batch else "download_")
    result.record(
        "plan",
        faults=[variant],
        cohort_ids=[operation_id],
        transport="httpx",
        public_entry="assembled asset service batch" if batch else "artifacts.download_infographic",
        fixture="android_artifacts._artifact and generated valid 1x1 PNG",
        operation_timeout=0.4 if variant == "body_stall" else 2.0,
        budgets={
            "baseline_timeout_s": 2.0,
            "rpc_timeout_s": 2.0,
            "fault_operation_timeout_s": 0.4 if variant == "body_stall" else 2.0,
            "cleanup_timeout_s": 2.0,
        },
        required_checks=[
            "fixture_decoded",
            "expected_outcome",
            "publication",
            "staging_clean",
            "resources_released",
            "recovered",
            "cleanup",
            "scripts_consumed",
            "capability_hops_credential_free",
            "initial_bearer_scoped",
        ],
        invariants=["I2", "I3", "I4", "I6", "I7", "I8"],
    )
    server = HttpFaultServer(hosts=[_HOST, _TARGET.host])
    rpc = GrpcFaultServer()
    if not batch:
        rpc.plan(LIST_ARTIFACTS, *(reply(_listing()) for _ in range(3)))
    good = Reply(body=_PAYLOAD, headers={"Content-Type": "image/png"})
    server.enqueue(_ASSET, good)
    if variant in {"401", "403"}:
        server.enqueue(_ASSET, Reply(int(variant)))
    elif variant in {"truncation", "prefix_disconnect"}:
        server.enqueue(
            _ASSET,
            Truncate(
                _PAYLOAD[:20] if variant == "prefix_disconnect" else _PAYLOAD, len(_PAYLOAD) + 100
            ),
        )
    elif variant in {"body_stall", "cancel", "close_reopen"}:
        server.enqueue(_ASSET, Stall("body", "held", good, prefix=_PAYLOAD[:20]))
    elif variant in {"html", "wrong_signature"}:
        server.enqueue(
            _ASSET,
            Reply(
                body=b"<html>error</html>" if variant == "html" else b"invalid image",
                headers={"Content-Type": "text/html" if variant == "html" else "image/png"},
            ),
        )
    elif variant in {"trusted_redirect", "expired_capability", "bearer_bounce"}:
        server.enqueue(_ASSET, Reply(302, headers={"Location": _TARGET_URL}))
        if variant == "expired_capability":
            server.enqueue(_TARGET, Reply(403))
        elif variant == "bearer_bounce":
            server.enqueue(
                _TARGET, Reply(302, headers={"Location": f"https://{_HOST}/bounce-image"})
            )
            server.enqueue(_BOUNCE, good)
        else:
            server.enqueue(_TARGET, good)
    elif variant == "untrusted_redirect":
        server.enqueue(
            _ASSET, Reply(302, headers={"Location": "https://unmapped.example/forbidden"})
        )
    elif variant == "redirect_loop":
        server.enqueue(_ASSET, *(Reply(302, headers={"Location": _URL}) for _ in range(21)))
    else:
        server.enqueue(_ASSET, good)
    server.enqueue(_ASSET, good)
    harness = None
    owned: list[asyncio.Task[Any]] = []
    try:
        async with android_cohort(
            result,
            rpc,
            server,
            lambda: build_android_client(rpc, http_server=server, timeout=2),
            release=lambda: server.release("held"),
            tasks=owned,
        ) as harness:
            client = harness.client
            with tempfile.TemporaryDirectory(prefix="fault-download-") as directory:
                path = Path(directory) / "image.png"
                assets = client._android_runtime.asset_downloads

                async def download(*, fault: bool = False) -> Any:
                    timeout = 0.4 if fault and variant == "body_stall" else 2.0
                    async with client.operation(timeout=timeout):
                        if batch:
                            return await assets.download_urls_batch([(_URL, str(path))])
                        return await client.artifacts.download_infographic(
                            _NOTEBOOK, str(path), artifact_id="image"
                        )

                baseline = await download()
                result.require(
                    "fixture_decoded",
                    path.read_bytes() == _PAYLOAD
                    and (not batch or baseline.succeeded == [str(path)]),
                )
                path.write_bytes(b"old destination")
                task = asyncio.create_task(download(fault=True))
                owned.append(task)
                if variant in {"cancel", "close_reopen"}:
                    await server.wait_for_event("response_prefix")
                    if variant == "cancel":
                        task.cancel()
                    else:
                        await asyncio.wait_for(client.close(drain=False), 2)
                error: BaseException | None = None
                outcome = None
                try:
                    outcome = await asyncio.wait_for(task, 4)
                except (Exception, asyncio.CancelledError) as exc:
                    error = exc
                if batch and error is None and outcome.failed:
                    error = outcome.failed[0][1]
                success = variant in {"success", "trusted_redirect", "bearer_bounce"}
                if success:
                    expected = error is None
                elif variant == "cancel":
                    expected = isinstance(error, asyncio.CancelledError)
                elif variant == "close_reopen":
                    expected = isinstance(error, (asyncio.CancelledError, RuntimeError))
                elif variant == "body_stall":
                    expected = isinstance(error, OperationTimeoutError)
                elif variant in {"401", "403", "expired_capability"}:
                    expected = isinstance(error, AuthError)
                else:
                    expected = isinstance(error, ArtifactDownloadError)
                result.record("outcome", error=None if error is None else type(error).__name__)
                result.require("expected_outcome", expected)
                result.require(
                    "publication",
                    path.read_bytes() == (_PAYLOAD if success else b"old destination"),
                )
                result.require("staging_clean", list(Path(directory).iterdir()) == [path])
                result.require("resources_released", not assets._clients and not assets._tasks)
                server.release("held")
                if variant == "close_reopen":
                    await client.__aenter__()
                recovered = await download()
                result.require(
                    "recovered",
                    path.read_bytes() == _PAYLOAD
                    and (not batch or recovered.succeeded == [str(path)]),
                )
                hops = [r for r in server.journal if r.route in {_TARGET, _BOUNCE}]
                result.require(
                    "capability_hops_credential_free",
                    all("authorization" not in r.headers for r in hops),
                )
                initial = [r for r in server.journal if r.route == _ASSET]
                result.require(
                    "initial_bearer_scoped",
                    all(
                        r.headers.get("authorization", "").startswith("Bearer fault-bearer-")
                        for r in initial
                    ),
                )
            await rpc.wait_for_idle()
            rpc.assert_consumed()
            server.assert_drained()
            result.require("scripts_consumed", True)
    finally:
        result.record(
            "http_trace",
            events=server.events,
            requests=[
                {
                    "route": "initial" if r.route == _ASSET else "capability_hop",
                    "method": r.route.method,
                    "bearer_attached": "authorization" in r.headers,
                }
                for r in server.journal
            ],
            digest=hashlib.sha256(_PAYLOAD).hexdigest(),
        )
    return result
