"""Public Web transfer cohorts with real socket and publication evidence.

Wire provenance: test_source_upload_pipeline registration shapes;
unit/test_artifact_downloads audio rows; unit/test_streaming_chat_wire frames.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import threading
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from notebooklm import NetworkError, ServerError
from notebooklm.exceptions import ArtifactDownloadError, AuthError
from notebooklm.rpc import RPCMethod

from .common import ScenarioResult
from .http import Disconnect, HttpFaultServer, Reply, Route, Stall, Transfer, Truncate
from .web import COOKIE_NAME, OLD_COOKIE, list_response, rpc_response

NOTEBOOK = "00000000-0000-4000-8000-000000000200"
SOURCE = "00000000-0000-4000-8000-000000000201"
LIST_ASSETS = Route.rpc(RPCMethod.LIST_ARTIFACTS.value)
GET_NOTEBOOK = Route.rpc(RPCMethod.GET_NOTEBOOK.value)
REGISTER = Route.rpc(RPCMethod.ADD_SOURCE_FILE.value)
READ = Route.rpc(RPCMethod.LIST_NOTEBOOKS.value)
UPLOAD = Route("POST", "notebook.google.com", "/upload/_/")
FINAL = Route("POST", "notebook.google.com", "/upload/_/", upload_id="fault-upload-secret")
BASE_FINAL = Route("POST", "notebook.google.com", "/upload/_/", upload_id="baseline-upload-secret")
ASSET = Route("GET", "lh3.googleusercontent.com", "/fault-asset")
TARGET = Route("GET", "storage.googleapis.com", "/fault-capability")
ASSET_URL = "https://lh3.googleusercontent.com/fault-asset?capability=fault-signed-secret"
TARGET_URL = "https://storage.googleapis.com/fault-capability?capability=fault-target-secret"
SESSION_URL = "https://notebook.google.com/upload/_/?upload_id=fault-upload-secret"
# Small WAV with a real RIFF/WAVE header and a 2-byte PCM sample.
MEDIA = bytes.fromhex(
    "524946462600000057415645666d74201000000001000100401f0000803e00000200100064617461020000000000"
)


class _ObservedClient:
    """Observe actual generator-owned files while retaining the real transport."""

    def __init__(self, client: httpx.AsyncClient, bodies: list[Any]) -> None:
        self._client = client
        self._bodies = bodies

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def __aenter__(self) -> _ObservedClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.__aexit__(*args)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        # The real Web body iterator closes over the opened descriptor. Keep
        # only that object for settlement checks; never replace or consume it.
        frame = getattr(kwargs.get("content"), "ag_frame", None)
        body = None if frame is None else frame.f_locals.get("file_obj")
        if body is not None:
            self._bodies.append(body)
        return await self._client.post(url, **kwargs)


class _GatedEnterClient:
    """Hold one constructed real client before finalize's dispatch checkpoint."""

    def __init__(self, client: httpx.AsyncClient, entered: asyncio.Event, release: asyncio.Event):
        self._client = client
        self._entered = entered
        self._release = release

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def __aenter__(self) -> httpx.AsyncClient:
        self._entered.set()
        try:
            await self._release.wait()
            return await self._client.__aenter__()
        except BaseException:
            await self._client.aclose()
            raise

    async def __aexit__(self, *args: Any) -> None:
        await self._client.__aexit__(*args)


async def _wait_for_finalize_shield(task: asyncio.Task[Any]) -> None:
    """Observe parent attachment after child publication, without a timed sleep."""
    for _ in range(100):
        awaitable: Any = task.get_coro()
        names = []
        while awaitable is not None:
            code = getattr(awaitable, "cr_code", None)
            if code is not None:
                names.append(code.co_name)
            awaitable = getattr(awaitable, "cr_await", None)
        if "upload_file_streaming" in names and "_spawn_transport_child" not in names:
            return
        await asyncio.sleep(0)
    raise AssertionError("finalize parent did not attach to its shield")


def _registration(source: str = SOURCE) -> Reply:
    return Reply(body=rpc_response(REGISTER.rpc_id or "", [[source]]))


def _start(*, baseline: bool = False) -> Reply:
    url = (
        SESSION_URL.replace("fault-upload-secret", "baseline-upload-secret")
        if baseline
        else SESSION_URL
    )
    return Reply(headers={"x-goog-upload-url": url})


def _finalize(payload: bytes, commit: str, response: Any = None) -> Transfer:
    return Transfer(
        response=Reply() if response is None else response,
        expected_size=len(payload),
        expected_digest=hashlib.sha256(payload).hexdigest(),
        commit_id=commit,
    )


def _probe(server: HttpFaultServer) -> None:
    server.enqueue(READ, Reply(body=list_response(READ.rpc_id or "", [("recovered", "Ready")])))


async def _recover(result: ScenarioResult, client: Any) -> None:
    notebooks = await client.notebooks.list()
    result.require("same_client_recovery", [item.id for item in notebooks] == ["recovered"])


def _transfer_trace(result: ScenarioResult, server: HttpFaultServer) -> None:
    result.record("transfer_trace", phases=server.events, commits=server.committed)
    result.require(
        "credential_policy_preserved",
        all(
            record.cookie_values.get(COOKIE_NAME) == OLD_COOKIE
            for record in server.journal
            if record.route in (REGISTER, UPLOAD, FINAL, BASE_FINAL)
        ),
    )


async def upload_case(result: ScenarioResult, variant: str) -> None:
    from .web_scenarios import _cohort, _requests, _require_clean

    baseline = b"baseline text\n"
    payload = b"transfer text\n" * (650_000 if variant == "body_stall" else 20_000)
    server = HttpFaultServer(max_body=16 * 1024 * 1024)
    server.enqueue(
        REGISTER,
        _registration("baseline-source"),
        Reply(503) if variant == "registration_failure" else _registration(),
    )
    server.enqueue(UPLOAD, _start(baseline=True))
    server.enqueue(BASE_FINAL, _finalize(baseline, "baseline"))
    if variant == "registration_failure":
        server.enqueue(
            GET_NOTEBOOK,
            Reply(body=rpc_response(GET_NOTEBOOK.rpc_id or "", [["Notebook", [], NOTEBOOK]])),
        )
    elif variant == "success":
        server.enqueue(UPLOAD, _start())
        server.enqueue(FINAL, _finalize(payload, "uploaded"))
    elif variant in {"start_failure", "start_401", "start_403"}:
        status = {"start_failure": 503, "start_401": 401, "start_403": 403}[variant]
        server.enqueue(UPLOAD, Reply(status))
    else:
        server.enqueue(UPLOAD, _start())
        if variant == "prefix_disconnect":
            action = Transfer(prefix_bytes=4096, disconnect_at="body_prefix")
        elif variant == "body_stall":
            action = Transfer(
                prefix_bytes=4096, gates={"body_prefix": "held-body"}, allow_abandoned_body=True
            )
        elif variant == "commit_loss":
            action = _finalize(payload, "uploaded", Disconnect())
        elif variant in {"finalize_401", "finalize_403"}:
            action = Transfer(response=Reply(int(variant.rsplit("_", 1)[1])))
        elif variant in {
            "cancel_before_dispatch",
            "cancel_after_prefix",
            "cancel_repeated",
            "close_reopen",
        }:
            action = Transfer(
                prefix_bytes=4096,
                gates={"body_prefix": "held-body"},
                expected_size=len(payload),
                expected_digest=hashlib.sha256(payload).hexdigest(),
                commit_id="uploaded",
                allow_abandoned_body=variant == "close_reopen",
            )
        else:
            raise ValueError(variant)
        server.enqueue(FINAL, Reply() if variant == "cancel_before_dispatch" else action)
    _probe(server)
    error: BaseException | None = None
    entered = asyncio.Event()
    entered_release = asyncio.Event()
    clients_created = 0
    observed_bodies: list[Any] = []

    def transfer_factory(**kwargs: Any) -> Any:
        nonlocal clients_created
        clients_created += 1
        client = server.client_factory(**kwargs)
        if variant == "cancel_before_dispatch" and clients_created == 4:
            return _GatedEnterClient(client, entered, entered_release)
        return _ObservedClient(client, observed_bodies)

    with tempfile.TemporaryDirectory(prefix="fault-web-upload-") as directory:
        source = Path(directory) / "source.txt"
        async with _cohort(
            result,
            server,
            transfer_timeout=0.3,
            record_sleep=False,
            transfer_client_factory=transfer_factory,
        ) as client:
            source.write_bytes(baseline)
            first = await client.sources.add_file(NOTEBOOK, source)
            result.require("successful_upload_baseline", first.id == "baseline-source")
            source.write_bytes(payload)
            upload_semaphore = client._web_runtime.source_uploader._upload_semaphore
            task = asyncio.create_task(client.sources.add_file(NOTEBOOK, source))
            if variant == "cancel_before_dispatch":
                await asyncio.wait_for(entered.wait(), 2.0)
                await _wait_for_finalize_shield(task)
                task.cancel()
            if variant in {"cancel_after_prefix", "cancel_repeated", "close_reopen"}:
                await server.wait_for_event("body_prefix")
                if variant in {"cancel_after_prefix", "cancel_repeated"}:
                    task.cancel()
                    # Shielded Web finalize still owns the request until it settles.
                    await asyncio.sleep(0)
                    result.require("cancel_waits_for_finalize", not task.done())
                    if variant == "cancel_repeated":
                        task.cancel()
                        await asyncio.sleep(0)
                        result.require("repeat_cancel_waits_for_finalize", not task.done())
                    server.release("held-body")
                else:
                    await client.close(drain=False)
                    server.release("held-body")
            try:
                uploaded = await task
            except BaseException as exc:
                error = exc
            if variant == "success":
                result.require("uploaded_identity", error is None and uploaded.id == SOURCE)
            elif variant in {
                "cancel_before_dispatch",
                "cancel_after_prefix",
                "cancel_repeated",
                "close_reopen",
            }:
                result.require("upload_cancelled", isinstance(error, asyncio.CancelledError))
                if variant == "close_reopen":
                    await client.__aenter__()
            elif variant == "registration_failure":
                result.require("registration_failure_propagated", isinstance(error, ServerError))
                result.require(
                    "no_fabricated_source_identity", getattr(error, "source_id", None) is None
                )
            else:
                expected = (
                    AuthError
                    if variant.endswith(("401", "403"))
                    else ServerError
                    if variant == "start_failure"
                    else NetworkError
                )
                result.require("stage_specific_error", isinstance(error, expected))
                result.require(
                    "registered_identity_retained", getattr(error, "source_id", None) == SOURCE
                )
                result.require(
                    "failure_stage_retained",
                    getattr(error, "stage", None)
                    == ("start_session" if variant.startswith("start_") else "upload_finalize"),
                )
            result.record("outcome", error=None if error is None else type(error).__name__)
            uploader = client._web_runtime.source_uploader
            result.require("upload_children_settled", not uploader._transport_tasks)
            result.require("upload_clients_settled", not uploader._transport_clients)
            expected_bodies = (
                1
                if variant.startswith("start_")
                or variant in {"registration_failure", "cancel_before_dispatch"}
                else 2
            )
            result.require("body_descriptors_observed", len(observed_bodies) == expected_bodies)
            result.require("body_descriptors_closed", all(body.closed for body in observed_bodies))
            result.require(
                "upload_permit_returned",
                upload_semaphore._value == uploader._max_concurrent_uploads,
            )
            await _recover(result, client)
            server.release("held-body")
        result.require("one_registration_per_upload", len(_requests(server, REGISTER)) == 2)
        expected_posts = (
            2 if variant == "registration_failure" else 3 if variant.startswith("start_") else 4
        )
        result.require(
            "no_duplicate_transfer",
            sum(len(_requests(server, route)) for route in (UPLOAD, FINAL, BASE_FINAL))
            == expected_posts,
        )
        expected_commit = variant in {
            "success",
            "commit_loss",
            "cancel_after_prefix",
            "cancel_repeated",
        }
        result.require(
            "independent_commit_evidence",
            server.committed == (["baseline", "uploaded"] if expected_commit else ["baseline"]),
        )
        if variant in {"prefix_disconnect", "body_stall"}:
            failed = _requests(server, FINAL)[-1]
            result.require("actual_partial_request", 0 < failed.body_bytes < len(payload))
        if variant == "cancel_before_dispatch":
            result.require(
                "scotty_cancel_observed",
                _requests(server, FINAL)[-1].headers.get("x-goog-upload-command") == "cancel",
            )
        _transfer_trace(result, server)
        _require_clean(result, server)


def _audio_rows(url: str = ASSET_URL) -> list[Any]:
    return [
        [
            "audio-fault",
            "Fault audio",
            1,
            None,
            3,
            None,
            [None, None, None, None, None, [[url, None, "audio/wav"]]],
        ]
    ]


async def _download(client: Any, destination: Path, *, batch: bool, url: str = ASSET_URL) -> Any:
    if batch:
        return await client.artifacts._asset_downloads.download_urls_batch(
            [(url, str(destination))]
        )
    return await client.artifacts.download_audio(
        NOTEBOOK, str(destination), artifact_id="audio-fault"
    )


async def download_case(result: ScenarioResult, variant: str, *, batch: bool = False) -> None:
    from .web_scenarios import _cohort, _requests, _require_clean

    server = HttpFaultServer(hosts=["lh3.googleusercontent.com", "storage.googleapis.com"])
    if not batch:
        server.enqueue(
            LIST_ASSETS,
            *[
                Reply(body=rpc_response(LIST_ASSETS.rpc_id or "", [_audio_rows()]))
                for _ in range(2)
            ],
        )
    good = Reply(body=MEDIA, headers={"content-type": "audio/wav"})
    server.enqueue(ASSET, good)
    if variant == "success":
        server.enqueue(ASSET, good)
    elif variant == "truncation":
        server.enqueue(ASSET, Truncate(MEDIA, len(MEDIA) + 20))
    elif variant == "prefix_disconnect":
        server.enqueue(ASSET, Truncate(MEDIA[:20], len(MEDIA)))
    elif variant in {"body_stall", "cancel", "close_reopen"}:
        server.enqueue(ASSET, Stall("body", "held-response", good, prefix=MEDIA[:20]))
    elif variant in {"401", "403", "expired_capability"}:
        server.enqueue(ASSET, Reply(401 if variant == "401" else 403))
    elif variant == "html":
        server.enqueue(
            ASSET, Reply(body=b"<html>expired</html>", headers={"content-type": "text/html"})
        )
    elif variant == "trusted_redirect":
        server.enqueue(ASSET, Reply(302, headers={"location": TARGET_URL}))
        server.enqueue(TARGET, good)
    elif variant == "untrusted_redirect":
        server.enqueue(ASSET, Reply(302, headers={"location": "https://untrusted.example/private"}))
    elif variant == "redirect_loop":
        server.enqueue(ASSET, *[Reply(302, headers={"location": ASSET_URL}) for _ in range(21)])
    else:
        raise ValueError(variant)
    _probe(server)
    successful = variant in {"success", "trusted_redirect"}
    error: BaseException | None = None
    responses: list[httpx.Response] = []
    response_headers = asyncio.Event()

    async def observe_response(response: httpx.Response) -> None:
        responses.append(response)
        if len(responses) >= 2:
            response_headers.set()

    def transfer_factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["timeout"] = httpx.Timeout(0.2)
        hooks = dict(kwargs.pop("event_hooks", {}))
        hooks["response"] = [*hooks.get("response", []), observe_response]
        return server.client_factory(event_hooks=hooks, **kwargs)

    with tempfile.TemporaryDirectory(prefix="fault-web-download-") as directory:
        destination = Path(directory) / f"{Path(directory).name}.wav"
        async with _cohort(
            result,
            server,
            transfer_timeout=0.2,
            record_sleep=False,
            transfer_client_factory=transfer_factory,
        ) as client:
            await _download(client, destination, batch=batch)
            result.require("successful_download_baseline", destination.read_bytes() == MEDIA)
            destination.write_bytes(b"old destination")
            task = asyncio.create_task(_download(client, destination, batch=batch))
            if variant in {"cancel", "close_reopen"}:
                await server.wait_for_event("response_prefix")
                await asyncio.wait_for(response_headers.wait(), 1)
                if variant == "cancel":
                    task.cancel()
                else:
                    await client.close(drain=False)
            returned: Any = None
            try:
                returned = await task
            except BaseException as exc:
                error = exc
            if variant == "close_reopen":
                await client.__aenter__()
            if successful:
                result.require(
                    "download_completed", error is None and destination.read_bytes() == MEDIA
                )
                result.record(
                    "integrity", bytes=len(MEDIA), sha256=hashlib.sha256(MEDIA).hexdigest()
                )
            else:
                if variant in {"cancel", "close_reopen"}:
                    valid_failure = isinstance(error, asyncio.CancelledError)
                elif variant in {"401", "403", "expired_capability"}:
                    valid_failure = isinstance(error, AuthError)
                elif batch:
                    valid_failure = (
                        error is None and len(returned.failed) == 1 and not returned.succeeded
                    )
                else:
                    valid_failure = isinstance(error, ArtifactDownloadError)
                result.require("download_failure_contract", valid_failure)
                result.require(
                    "old_destination_preserved", destination.read_bytes() == b"old destination"
                )
            result.record(
                "outcome",
                error=None if error is None else type(error).__name__,
                publication="buffered_batch" if batch else "streamed_single",
            )
            result.require("staging_removed", list(Path(directory).iterdir()) == [destination])
            result.require(
                "writer_settled",
                not any(
                    t.name.startswith(f"artifact-dl-writer-{destination.name}")
                    for t in threading.enumerate()
                ),
            )
            result.require("asset_clients_settled", not client.artifacts._asset_downloads._clients)
            result.require("asset_tasks_settled", not client.artifacts._asset_downloads._tasks)
            result.record(
                "response_cleanup",
                observed=len(responses),
                closed=[response.is_closed for response in responses],
            )
            result.require(
                "asset_responses_closed",
                len(responses) >= 2 and all(response.is_closed for response in responses),
            )
            await _recover(result, client)
            server.release("held-response")
        attempts = len(_requests(server, ASSET))
        result.require(
            "bounded_asset_requests", attempts == (22 if variant == "redirect_loop" else 2)
        )
        if variant == "trusted_redirect":
            result.require(
                "trusted_hop_received_no_google_cookie",
                not _requests(server, TARGET)[0].cookie_values,
            )
        result.require(
            "asset_hop_cookie_policy",
            all(COOKIE_NAME not in r.cookie_values for r in _requests(server, ASSET)),
        )
        _transfer_trace(result, server)
        _require_clean(result, server)


async def credential_redirect_case(result: ScenarioResult, *, trusted: bool, batch: bool) -> None:
    from .web_scenarios import _cohort, _requests, _require_clean

    initial = Route("GET", "notebook.google.com", "/fault-cookie-asset")
    initial_url = "https://notebook.google.com/fault-cookie-asset?capability=fault-cookie-secret"
    server = HttpFaultServer(hosts=["storage.googleapis.com"])
    good = Reply(body=MEDIA, headers={"content-type": "audio/wav"})
    server.enqueue(
        initial,
        good,
        Reply(
            302, headers={"location": TARGET_URL if trusted else "https://untrusted.example/asset"}
        ),
    )
    if trusted:
        server.enqueue(TARGET, good)
    if not batch:
        server.enqueue(
            LIST_ASSETS,
            *[
                Reply(body=rpc_response(LIST_ASSETS.rpc_id or "", [_audio_rows(initial_url)]))
                for _ in range(2)
            ],
        )
    _probe(server)
    with tempfile.TemporaryDirectory(prefix="fault-web-credentials-") as directory:
        destination = Path(directory) / "asset.wav"
        async with _cohort(result, server, transfer_timeout=0.2, record_sleep=False) as client:
            await _download(client, destination, batch=batch, url=initial_url)
            result.require("credential_download_baseline", destination.read_bytes() == MEDIA)
            destination.write_bytes(b"existing")
            error: BaseException | None = None
            outcome = None
            try:
                outcome = await _download(client, destination, batch=batch, url=initial_url)
            except BaseException as exc:
                error = exc
            if trusted:
                result.require(
                    "trusted_redirect_succeeded",
                    error is None and destination.read_bytes() == MEDIA,
                )
                result.require(
                    "capability_hop_uncredentialed", not _requests(server, TARGET)[0].cookie_values
                )
            else:
                result.require(
                    "untrusted_redirect_refused",
                    (error is None and len(outcome.failed) == 1)
                    if batch
                    else isinstance(error, ArtifactDownloadError),
                )
                result.require("untrusted_hop_never_dispatched", not _requests(server, TARGET))
                result.require(
                    "credential_failure_preserves_file", destination.read_bytes() == b"existing"
                )
            result.require(
                "initial_hop_received_live_cookie",
                len(_requests(server, initial)) == 2
                and all(
                    record.cookie_values.get(COOKIE_NAME) == OLD_COOKIE
                    for record in _requests(server, initial)
                ),
            )
            await _recover(result, client)
        _transfer_trace(result, server)
        _require_clean(result, server)


UPLOAD_VARIANTS = (
    "registration_failure",
    "cancel_before_dispatch",
    "cancel_repeated",
    "success",
    "start_failure",
    "prefix_disconnect",
    "body_stall",
    "commit_loss",
    "start_401",
    "start_403",
    "finalize_401",
    "finalize_403",
    "cancel_after_prefix",
    "close_reopen",
)
DOWNLOAD_VARIANTS = (
    "success",
    "truncation",
    "prefix_disconnect",
    "body_stall",
    "cancel",
    "close_reopen",
    "401",
    "403",
    "expired_capability",
    "html",
    "trusted_redirect",
    "untrusted_redirect",
    "redirect_loop",
)
IMPLEMENTATIONS = {
    **{
        f"download_{'batch_' if batch else ''}cookie_{'trusted' if trusted else 'untrusted'}_redirect": partial(
            credential_redirect_case, trusted=trusted, batch=batch
        )
        for trusted in (True, False)
        for batch in (True, False)
    },
    **{f"upload_{case}": partial(upload_case, variant=case) for case in UPLOAD_VARIANTS},
    **{f"download_{case}": partial(download_case, variant=case) for case in DOWNLOAD_VARIANTS},
    **{
        f"download_batch_{case}": partial(download_case, variant=case, batch=True)
        for case in DOWNLOAD_VARIANTS
    },
}
PLANS = {
    name: (("transfer:success-baseline", name, "same-client:recovery"), 1)
    for name in IMPLEMENTATIONS
}
