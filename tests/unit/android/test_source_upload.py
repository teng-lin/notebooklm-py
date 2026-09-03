"""Offline Android file-upload wire, lifecycle, and security tests."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import traceback
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest

from notebooklm._android import drive_staging as drive_staging_module
from notebooklm._android import upload as upload_module
from notebooklm._android.auth import BearerCredential, BearerProvider
from notebooklm._android.drive_staging import (
    _DRIVE_STAGED_UPLOAD_EXTENSIONS,
    _NATIVE_UPLOAD_EXTENSIONS,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
    sources_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.v1 import source_settings_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._android.sources import (
    ADD_SOURCES_METHOD,
    ADD_TENTATIVE_SOURCES_METHOD,
    GET_PROJECT_METHOD,
    MUTATE_SOURCE_METHOD,
    AndroidSourcesAPI,
)
from notebooklm._android.upload import (
    UPLOAD_ORIGIN,
    AndroidUploadPipeline,
    _resolve_upload_content_type,
    build_upload_start_body,
    validate_upload_session_url,
)
from notebooklm._curl_cffi_transport import CurlCffiAsyncClient
from notebooklm._types.sources import _UPLOAD_FILE_EXTENSIONS
from notebooklm.exceptions import (
    AuthError,
    NetworkError,
    RPCError,
    ServerError,
    SourceAddError,
    SourceProcessingError,
    SourceTimeoutError,
    ValidationError,
)
from notebooklm.types import Source, SourceStatus

NOTEBOOK_ID = "00000000-0000-4000-8000-000000000200"
DRIVE_STAGED_ID = "drive-staged-file-id"
DRIVE_STAGING_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
SOURCE_ID = "00000000-0000-4000-8000-000000000201"
SESSION_URL = (
    f"https://notebooklm-pa.googleapis.com/upload/upload/{NOTEBOOK_ID}"
    "?upload_id=session-capability&upload_protocol=resumable"
)
_READ = cast(Any, read_pb2)
_WRITE = cast(Any, sources_pb2)
_SETTINGS = cast(Any, source_settings_pb2)


@dataclass(frozen=True)
class _Lease:
    epoch: int


class FakeSession:
    def __init__(self) -> None:
        self.epoch = 7
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.handlers: dict[str, Any] = {}
        self.scopes: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        self.scopes.append(label)
        yield _Lease(self.epoch)

    def assert_epoch(self, expected_epoch: int) -> None:
        if expected_epoch != self.epoch:
            raise RuntimeError("retired fake epoch")

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        result = self.handlers[method]
        if isinstance(result, deque):
            result = result.popleft()
        if callable(result):
            result = result(request, kwargs)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeBearerProvider:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.invalidated: list[int] = []

    async def get(self, expected_epoch: int) -> BearerCredential:
        self.calls.append(expected_epoch)
        generation = len(self.calls)
        return BearerCredential(token=f"bearer-secret-{generation}", generation=generation)

    def invalidate(self, generation: int) -> None:
        self.invalidated.append(generation)


@dataclass
class _HTTPCall:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes
    follow_redirects: bool


class HTTPHarness:
    def __init__(self) -> None:
        self.start_status = 200
        self.start_headers: list[tuple[str, str]] = [
            ("X-Goog-Upload-Status", "active"),
            ("X-Goog-Upload-URL", SESSION_URL),
        ]
        self.final_status = 200
        self.final_headers: list[tuple[str, str]] = [("X-Goog-Upload-Status", "final")]
        self.calls: list[_HTTPCall] = []
        self.factory_kwargs: list[dict[str, Any]] = []
        self.clients: list[FakeHTTPClient] = []
        self.post_started = asyncio.Event()
        self.put_started = asyncio.Event()
        self.block_post: asyncio.Event | None = None
        self.block_put: asyncio.Event | None = None
        self.put_error: BaseException | None = None
        # Drive staging round-trip (``.docx``).
        self.drive_stage_status = 200
        self.drive_stage_error: BaseException | None = None
        self.drive_stage_body: dict[str, Any] | None = {"id": DRIVE_STAGED_ID}
        self.drive_delete_status = 204
        self.drive_delete_error: BaseException | None = None

    def factory(self, **kwargs: Any) -> FakeHTTPClient:
        self.factory_kwargs.append(kwargs)
        client = FakeHTTPClient(self)
        self.clients.append(client)
        return client


class FakeHTTPClient:
    def __init__(self, harness: HTTPHarness) -> None:
        self.harness = harness
        self.closed = False

    async def __aenter__(self) -> FakeHTTPClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self.closed = True

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes,
        follow_redirects: bool,
    ) -> httpx.Response:
        self.harness.post_started.set()
        self.harness.calls.append(
            _HTTPCall("POST", url, dict(headers), bytes(content), follow_redirects)
        )
        if url.startswith(DRIVE_STAGING_UPLOAD_URL):
            if self.harness.drive_stage_error is not None:
                raise self.harness.drive_stage_error
            return httpx.Response(
                self.harness.drive_stage_status,
                json=self.harness.drive_stage_body,
                request=httpx.Request("POST", url),
            )
        if self.harness.block_post is not None:
            await self.harness.block_post.wait()
        return httpx.Response(
            self.harness.start_status,
            headers=self.harness.start_headers,
            request=httpx.Request("POST", url),
        )

    async def delete(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        follow_redirects: bool,
    ) -> httpx.Response:
        self.harness.calls.append(_HTTPCall("DELETE", url, dict(headers), b"", follow_redirects))
        if self.harness.drive_delete_error is not None:
            raise self.harness.drive_delete_error
        return httpx.Response(
            self.harness.drive_delete_status,
            request=httpx.Request("DELETE", url),
        )

    async def put(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        content: Any,
        follow_redirects: bool,
    ) -> httpx.Response:
        self.harness.put_started.set()
        if self.harness.block_put is not None:
            await self.harness.block_put.wait()
        if self.harness.put_error is not None:
            raise self.harness.put_error
        body = bytearray()
        async for chunk in content:
            body.extend(chunk)
        self.harness.calls.append(
            _HTTPCall("PUT", url, dict(headers), bytes(body), follow_redirects)
        )
        return httpx.Response(
            self.harness.final_status,
            headers=self.harness.final_headers,
            request=httpx.Request("PUT", url),
        )


class FakeCurlHTTPClient(CurlCffiAsyncClient):
    """Curl-branch request spy without importing or constructing curl_cffi."""

    def __init__(self, harness: HTTPHarness) -> None:
        self.harness = harness
        self.closed = False

    async def __aenter__(self) -> FakeCurlHTTPClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self.closed = True

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs["headers"])
        self.harness.calls.append(
            _HTTPCall(
                "POST",
                url,
                headers,
                bytes(kwargs["content"]),
                kwargs["follow_redirects"],
            )
        )
        return httpx.Response(
            self.harness.start_status,
            headers=self.harness.start_headers,
            request=httpx.Request("POST", url),
        )

    async def stream_upload(
        self,
        url: str,
        source: Any,
        *,
        total_bytes: int,
        headers: Mapping[str, str],
        method: str = "POST",
        on_chunk: Callable[[int], Awaitable[None]] | None = None,
        overall_timeout: float | None = None,
        stop_on_cancel: bool = False,
    ) -> httpx.Response:
        assert overall_timeout is not None
        assert stop_on_cancel is True
        body = bytearray()
        while chunk := source.read(65536):
            body.extend(chunk)
            if on_chunk is not None:
                await on_chunk(len(chunk))
        assert len(body) == total_bytes
        self.harness.calls.append(_HTTPCall(method, url, dict(headers), bytes(body), False))
        return httpx.Response(
            self.harness.final_status,
            headers=self.harness.final_headers,
            request=httpx.Request(method, url),
        )


def _pdf_source(
    status: int,
    *,
    title: str = "document.pdf",
) -> Any:
    return _READ.Source(
        source_id=_READ.SourceId(id=SOURCE_ID),
        title=title,
        metadata=_READ.SourceMetadata(original_source_content_type=_READ.SOURCE_CONTENT_TYPE_PDF),
        settings=_SETTINGS.SourceSettings(status=status),
    )


def _project(status: int, *, title: str = "document.pdf") -> Any:
    return _READ.GetProjectResponse(
        project=_READ.Project(
            id=NOTEBOOK_ID,
            title="Notebook",
            sources=[_pdf_source(status, title=title)],
        )
    )


async def _graph(
    harness: HTTPHarness,
    *,
    upload_timeout: float = 2.0,
    curl: bool = False,
    monotonic: Callable[[], float] | None = None,
) -> tuple[FakeSession, FakeBearerProvider, AndroidUploadPipeline, AndroidSourcesAPI]:
    session = FakeSession()
    bearer = FakeBearerProvider()

    def _registration(request: Any, kwargs: dict[str, Any]) -> Any:
        assert kwargs["replay_safe"] is False
        assert kwargs["expected_epoch"] == session.epoch
        filename = request.tentative_sources_metadata[0].name
        return _WRITE.AddTentativeSourcesResponse(
            tentative_sources=[_pdf_source(0, title=filename)]
        )

    session.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration
    session.handlers[GET_PROJECT_METHOD] = _project(_SETTINGS.SOURCE_STATUS_COMPLETE)
    session.handlers[MUTATE_SOURCE_METHOD] = _WRITE.MutateSourceResponse()
    session.handlers[ADD_SOURCES_METHOD] = lambda request, _kwargs: _WRITE.AddSourcesResponse(
        sources=[_pdf_source(0)]
    )

    factory: Callable[..., Any]
    if curl:

        def factory(**kwargs: Any) -> FakeCurlHTTPClient:
            harness.factory_kwargs.append(kwargs)
            client = FakeCurlHTTPClient(harness)
            harness.clients.append(cast(FakeHTTPClient, client))
            return client
    else:
        factory = harness.factory

    pipeline = AndroidUploadPipeline(
        session=cast(AndroidSession, session),
        bearer_provider=cast(BearerProvider, bearer),
        upload_timeout=upload_timeout,
        async_client_factory=factory,
    )
    loop = asyncio.get_running_loop()
    pipeline.set_bound_loop(loop)
    pipeline.reset_after_open()
    await pipeline.open(loop, session.epoch)
    api = (
        AndroidSourcesAPI(cast(AndroidSession, session), pipeline)
        if monotonic is None
        else AndroidSourcesAPI(cast(AndroidSession, session), pipeline, monotonic=monotonic)
    )
    return session, bearer, pipeline, api


def _write_pdf(tmp_path: Path, size: int = 140_000) -> tuple[Path, bytes]:
    content = b"%PDF-1.7\n" + b"x" * (size - 9)
    path = tmp_path / "document.pdf"
    path.write_bytes(content)
    return path, content


@pytest.mark.asyncio
@pytest.mark.parametrize("curl", [False, True], ids=["httpx", "curl-cffi"])
async def test_pdf_synthetic_branch_pins_wire_headers_body_progress_and_fresh_bearers(
    tmp_path: Path,
    curl: bool,
) -> None:
    harness = HTTPHarness()
    session, bearer, _, api = await _graph(harness, curl=curl)
    path, content = _write_pdf(tmp_path)
    progress: list[tuple[int, int]] = []

    result = await api.add_file(
        NOTEBOOK_ID,
        path,
        on_progress=lambda sent, total: progress.append((sent, total)),
    )

    assert result.id == SOURCE_ID
    assert result.title == path.name
    assert result.status is SourceStatus.PROCESSING
    assert session.scopes == ["Android source upload"]
    assert [call[0] for call in session.calls] == [ADD_TENTATIVE_SOURCES_METHOD]
    registration = session.calls[0][1]
    assert registration.tentative_sources_metadata[0].name == path.name
    assert registration.request_context.client_type == 3
    assert registration.request_context.client_metadata.client_version == "1.46.7.940945420"
    assert registration.provenance.origin_product_type == 1
    assert bearer.calls == [7, 7]
    assert bearer.invalidated == []
    assert [call.method for call in harness.calls] == ["POST", "PUT"]
    start, final = harness.calls
    assert start.url == f"https://notebooklm-pa.googleapis.com/upload/upload/{NOTEBOOK_ID}"
    assert start.follow_redirects is False
    assert json.loads(start.body) == json.loads(build_upload_start_body(NOTEBOOK_ID, SOURCE_ID))
    assert start.headers == {
        "Authorization": "Bearer bearer-secret-1",
        "Content-Type": "text/plain; charset=utf-8",
        "User-Agent": "NotebookLM/1.46.7.940945420 (Android 16; sdk_gphone64_arm64)",
        "X-Goog-AuthUser": "0",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Content-Length": str(len(content)),
        "X-Goog-Upload-File-Name": path.name,
        "X-Goog-Upload-Header-Content-Length": str(len(content)),
        "X-Goog-Upload-Header-Content-Type": "application/pdf",
        "X-Goog-Upload-Protocol": "resumable",
    }
    assert final.url == SESSION_URL
    assert final.body == content
    assert final.headers == {
        "Authorization": "Bearer bearer-secret-2",
        "User-Agent": "Dart/3.13 (dart:io)",
        "Content-Length": str(len(content)),
        "X-Goog-AuthUser": "0",
        "X-Goog-Upload-Command": "upload, finalize",
        "X-Goog-Upload-Offset": "0",
    }
    assert progress[-1] == (len(content), len(content))
    assert [sent for sent, _ in progress] == sorted(sent for sent, _ in progress)
    assert all(client.closed for client in harness.clients)
    assert all(kwargs["follow_redirects"] is False for kwargs in harness.factory_kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wait", "custom_title", "raw_status", "expected_status", "expect_mutation"),
    [
        (False, "Custom", _SETTINGS.SOURCE_STATUS_PENDING, SourceStatus.PROCESSING, True),
        (True, None, _SETTINGS.SOURCE_STATUS_COMPLETE, SourceStatus.READY, False),
        (True, "Custom", _SETTINGS.SOURCE_STATUS_COMPLETE, SourceStatus.READY, True),
    ],
)
async def test_waiting_branches_use_one_exact_read_then_optional_no_readback_title(
    tmp_path: Path,
    wait: bool,
    custom_title: str | None,
    raw_status: int,
    expected_status: SourceStatus,
    expect_mutation: bool,
) -> None:
    harness = HTTPHarness()
    session, _, _, api = await _graph(harness)
    path, _ = _write_pdf(tmp_path)
    session.handlers[GET_PROJECT_METHOD] = _project(raw_status)

    result = await api.add_file(
        NOTEBOOK_ID,
        path,
        wait=wait,
        wait_timeout=0.2,
        title=custom_title,
    )

    assert result.status is expected_status
    # Gate 3: the GetProject row uses the same public PDF type code as the web
    # source plane, so fail-fast/transient policy sees backend-parity metadata.
    assert result._type_code == 3
    assert result.title == (custom_title or path.name)
    methods = [call[0] for call in session.calls]
    assert methods.count(GET_PROJECT_METHOD) == 1
    assert methods.count(MUTATE_SOURCE_METHOD) == int(expect_mutation)
    if expect_mutation:
        assert methods[-1] == MUTATE_SOURCE_METHOD
        assert session.calls[-1][2]["replay_safe"] is False
        assert session.calls[-1][2]["expected_epoch"] == 7


@pytest.mark.asyncio
async def test_error_or_wait_timeout_never_dispatches_title_mutation(tmp_path: Path) -> None:
    path, _ = _write_pdf(tmp_path)
    # A tight budget is safe here: ``_wait_uploaded_source`` polls before it
    # consults the deadline, so the ERROR leg always reaches its first look
    # (pinned with a stepping clock in
    # ``test_upload_wait_always_looks_once_before_declaring_timeout``).
    wait_timeout = 0.01
    for response, expected in [
        (_project(_SETTINGS.SOURCE_STATUS_ERROR), SourceProcessingError),
        (_project(_SETTINGS.SOURCE_STATUS_TENTATIVE), SourceTimeoutError),
    ]:
        harness = HTTPHarness()
        session, _, _, api = await _graph(harness)
        session.handlers[GET_PROJECT_METHOD] = response
        with pytest.raises(expected):
            await api.add_file(
                NOTEBOOK_ID,
                path,
                wait=False,
                wait_timeout=wait_timeout,
                title="Custom",
            )
        assert MUTATE_SOURCE_METHOD not in [call[0] for call in session.calls]


def _stepping_clock(*readings: float) -> Callable[[], float]:
    """A monotonic clock that returns each reading once, then the last forever."""
    queue = deque(readings)

    def read() -> float:
        if len(queue) > 1:
            return queue.popleft()
        return queue[0]

    return read


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        (_SETTINGS.SOURCE_STATUS_ERROR, SourceProcessingError),
        (_SETTINGS.SOURCE_STATUS_TENTATIVE, SourceTimeoutError),
    ],
    ids=["error", "tentative"],
)
async def test_upload_wait_always_looks_once_before_declaring_timeout(
    tmp_path: Path,
    raw_status: int,
    expected: type[Exception],
) -> None:
    """A positive budget buys one GetProject even if the clock already spent it.

    The clock reads 0.0 when the deadline starts and 1.0 on every read after,
    so a 10 ms budget is gone before the first ``expired()`` check — the shape
    of one coarse ``GetTickCount64`` tick on Windows before 3.13. The loop must
    still look: a server-side ERROR is reported as ``SourceProcessingError``,
    and a timeout carries the status it actually observed, not ``None``. The
    single look also gets a real wire budget rather than the ``0.0`` that
    ``remaining()`` reads on that tick.
    """
    harness = HTTPHarness()
    session, _, _, api = await _graph(harness, monotonic=_stepping_clock(0.0, 1.0))
    session.handlers[GET_PROJECT_METHOD] = _project(raw_status)
    path, _ = _write_pdf(tmp_path)

    with pytest.raises(expected) as captured:
        await api.add_file(NOTEBOOK_ID, path, wait=False, wait_timeout=0.01, title="Custom")

    methods = [call[0] for call in session.calls]
    assert methods.count(GET_PROJECT_METHOD) == 1
    assert MUTATE_SOURCE_METHOD not in methods
    poll_kwargs = next(call[2] for call in session.calls if call[0] == GET_PROJECT_METHOD)
    assert poll_kwargs["timeout"] == pytest.approx(0.01)
    if expected is SourceTimeoutError:
        assert captured.value.last_status == raw_status


@pytest.mark.asyncio
async def test_upload_wait_zero_budget_never_polls(tmp_path: Path) -> None:
    """``wait_timeout=0`` is an explicit "do not wait": no look, plain timeout."""
    harness = HTTPHarness()
    session, _, _, api = await _graph(harness, monotonic=_stepping_clock(0.0))
    session.handlers[GET_PROJECT_METHOD] = _project(_SETTINGS.SOURCE_STATUS_ERROR)
    path, _ = _write_pdf(tmp_path)

    with pytest.raises(SourceTimeoutError) as captured:
        await api.add_file(NOTEBOOK_ID, path, wait=False, wait_timeout=0.0, title="Custom")

    assert GET_PROJECT_METHOD not in [call[0] for call in session.calls]
    assert captured.value.last_status is None


@pytest.mark.asyncio
async def test_upload_wait_never_polls_past_the_deadline_after_sleeping(
    tmp_path: Path,
) -> None:
    """Only the first look may bypass the deadline; a spent sleep ends the wait.

    start=0.0 against a 30 s budget; the first look reads 10.0 elapsed and gets
    ``remaining()`` as its wire budget, then the post-sleep check reads 40.0 —
    the loop must raise without issuing a second, post-deadline GetProject.
    """
    harness = HTTPHarness()
    clock = _stepping_clock(0.0, 10.0, 10.0, 10.0, 40.0)
    session, _, _, api = await _graph(harness, monotonic=clock)
    session.handlers[GET_PROJECT_METHOD] = _project(_SETTINGS.SOURCE_STATUS_TENTATIVE)
    path, _ = _write_pdf(tmp_path)

    with pytest.raises(SourceTimeoutError):
        await api.add_file(NOTEBOOK_ID, path, wait=False, wait_timeout=30.0, title="Custom")

    budgets = [call[2]["timeout"] for call in session.calls if call[0] == GET_PROJECT_METHOD]
    assert budgets == [pytest.approx(20.0)]


@pytest.mark.asyncio
async def test_upload_wait_wire_budget_tracks_remaining_and_floors_near_expiry(
    tmp_path: Path,
) -> None:
    """A look that starts inside the budget is floored, never handed ~0.0.

    The second look begins with 0.2 s remaining of a 30 s budget; its wire
    budget is floored to ``_POLL_WIRE_FLOOR`` so the request actually goes out
    instead of being rejected pre-wire as an ``RPCTimeoutError``.
    """
    harness = HTTPHarness()
    clock = _stepping_clock(0.0, 10.0, 29.6, 29.6, 29.8, 29.8, 40.0)
    session, _, _, api = await _graph(harness, monotonic=clock)
    session.handlers[GET_PROJECT_METHOD] = _project(_SETTINGS.SOURCE_STATUS_TENTATIVE)
    path, _ = _write_pdf(tmp_path)

    with pytest.raises(SourceTimeoutError):
        await api.add_file(NOTEBOOK_ID, path, wait=False, wait_timeout=30.0, title="Custom")

    budgets = [call[2]["timeout"] for call in session.calls if call[0] == GET_PROJECT_METHOD]
    assert budgets == [pytest.approx(20.0), pytest.approx(1.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_status",
    [
        0,
        _SETTINGS.SOURCE_STATUS_TENTATIVE,
        _SETTINGS.SOURCE_STATUS_PENDING_DELETION,
        999,
    ],
)
async def test_unaccepted_registration_statuses_time_out_without_title_mutation(
    tmp_path: Path,
    raw_status: int,
) -> None:
    harness = HTTPHarness()
    session, _, _, api = await _graph(harness)
    session.handlers[GET_PROJECT_METHOD] = _project(raw_status)
    path, _ = _write_pdf(tmp_path)

    with pytest.raises(SourceTimeoutError):
        await api.add_file(
            NOTEBOOK_ID,
            path,
            wait=False,
            wait_timeout=0.01,
            title="Custom",
        )

    assert MUTATE_SOURCE_METHOD not in [call[0] for call in session.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rename_error",
    [RPCError("safe rpc failure", rpc_code=14), NetworkError("safe network failure")],
    ids=["rpc", "network"],
)
async def test_title_mutation_failure_is_best_effort_without_readback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    rename_error: Exception,
) -> None:
    harness = HTTPHarness()
    session, _, _, api = await _graph(harness)
    path, _ = _write_pdf(tmp_path)
    session.handlers[GET_PROJECT_METHOD] = _project(_SETTINGS.SOURCE_STATUS_PENDING)
    session.handlers[MUTATE_SOURCE_METHOD] = rename_error

    with caplog.at_level(logging.WARNING, logger="notebooklm._sources"):
        result = await api.add_file(NOTEBOOK_ID, path, title="Custom")

    assert result.title == path.name
    assert [call[0] for call in session.calls].count(GET_PROJECT_METHOD) == 1
    warnings = [
        record for record in caplog.records if "title finalization failed" in record.message
    ]
    assert len(warnings) == 1
    assert "Custom" not in caplog.text
    assert str(rename_error) not in caplog.text
    assert warnings[0].exc_info is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal",
    [asyncio.CancelledError(), RuntimeError("retired epoch")],
    ids=["cancellation", "lifecycle"],
)
async def test_title_cancellation_and_lifecycle_failure_propagate(
    tmp_path: Path,
    terminal: BaseException,
) -> None:
    harness = HTTPHarness()
    session, _, _, api = await _graph(harness)
    path, _ = _write_pdf(tmp_path)
    session.handlers[GET_PROJECT_METHOD] = _project(_SETTINGS.SOURCE_STATUS_PENDING)
    session.handlers[MUTATE_SOURCE_METHOD] = terminal

    with pytest.raises(type(terminal)):
        await api.add_file(NOTEBOOK_ID, path, title="Custom")

    assert [call[0] for call in session.calls].count(MUTATE_SOURCE_METHOD) == 1


def _assert_raw_upload_owners_absent_from_library_traceback(
    error: BaseException,
    *raw_objects: object,
) -> None:
    inspected: list[str] = []
    leaked: list[str] = []
    for frame, _line in traceback.walk_tb(error.__traceback__):
        source_path = frame.f_code.co_filename.replace("\\", "/")
        if "/src/notebooklm/" not in source_path:
            continue
        inspected.append(frame.f_code.co_name)
        if any(raw is value for raw in raw_objects for value in frame.f_locals.values()):
            leaked.append(frame.f_code.co_name)

    assert inspected, "no notebooklm frame was inspected; the scan proved nothing"
    assert not leaked, f"raw upload owner survived in library frames: {leaked}"


@pytest.mark.asyncio
async def test_post_upload_failure_traceback_retains_no_raw_upload_owner(tmp_path: Path) -> None:
    harness = HTTPHarness()
    session, bearer, pipeline, api = await _graph(harness)
    session.handlers[GET_PROJECT_METHOD] = _project(_SETTINGS.SOURCE_STATUS_ERROR)
    path, _ = _write_pdf(tmp_path)

    with pytest.raises(SourceProcessingError) as raised:
        await api.add_file(NOTEBOOK_ID, path, wait=True)

    error = raised.value
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_raw_upload_owners_absent_from_library_traceback(
        error,
        session,
        bearer,
        pipeline,
        api,
    )


@pytest.mark.asyncio
async def test_missing_file_and_blank_title_reject_before_bearer_or_wire(tmp_path: Path) -> None:
    harness = HTTPHarness()
    session, bearer, _, api = await _graph(harness)
    missing = tmp_path / "missing.txt"

    with pytest.raises(FileNotFoundError):
        await api.add_file(NOTEBOOK_ID, missing)
    with pytest.raises(ValidationError, match="Title cannot be empty"):
        await api.add_file(NOTEBOOK_ID, tmp_path / "missing.pdf", title="   ")
    with pytest.raises(FileNotFoundError):
        await api.add_file(NOTEBOOK_ID, tmp_path / "missing.pdf")

    assert session.calls == []
    assert bearer.calls == []
    assert harness.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "mime_type", "content", "expected_type"),
    [
        ("notes.txt", None, b"hello", "text/plain"),
        ("notes.md", None, b"# hi", "text/markdown"),
        ("book.epub", None, b"PK\x03\x04epub", "application/epub+zip"),
    ],
)
async def test_non_pdf_upload_preserves_type_and_length_on_android_wire(
    tmp_path: Path,
    filename: str,
    mime_type: str | None,
    content: bytes,
    expected_type: str,
) -> None:
    harness = HTTPHarness()
    _, _, _, api = await _graph(harness)
    path = tmp_path / filename
    path.write_bytes(content)

    result = await api.add_file(NOTEBOOK_ID, path, mime_type=mime_type)

    assert result.id == SOURCE_ID
    start = next(call for call in harness.calls if call.method == "POST")
    assert start.headers["X-Goog-Upload-Content-Length"] == str(len(content))
    assert start.headers["X-Goog-Upload-Header-Content-Length"] == str(len(content))
    assert start.headers["X-Goog-AuthUser"] == "0"
    assert start.headers["X-Goog-Upload-Header-Content-Type"] == expected_type
    final = next(call for call in harness.calls if call.method == "PUT")
    assert final.headers["X-Goog-AuthUser"] == "0"
    assert final.body == content


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["document.docx", "DOCUMENT.DOCX"])
async def test_public_compat_routes_only_docx_to_web(
    tmp_path: Path,
    filename: str,
) -> None:
    harness = HTTPHarness()
    session, _, pipeline, _ = await _graph(harness)
    path = tmp_path / filename
    path.write_bytes(b"compatibility payload")
    expected = Source(id=SOURCE_ID, title=filename, status=SourceStatus.READY)
    compat = AsyncMock(return_value=expected)
    progress = AsyncMock()
    api = AndroidSourcesAPI(
        cast(AndroidSession, session),
        pipeline,
        add_file_compat=compat,
    )

    result = await api.add_file(
        NOTEBOOK_ID,
        path,
        "application/custom",
        wait=True,
        wait_timeout=17.0,
        title="Requested title",
        on_progress=progress,
    )

    assert result is expected
    compat.assert_awaited_once_with(
        NOTEBOOK_ID,
        path,
        "application/custom",
        wait=True,
        wait_timeout=17.0,
        title="Requested title",
        on_progress=progress,
    )
    assert session.calls == []
    assert harness.calls == []


@pytest.mark.asyncio
async def test_public_compat_routes_from_canonical_symlink_target(tmp_path: Path) -> None:
    harness = HTTPHarness()
    session, _, pipeline, _ = await _graph(harness)
    target = tmp_path / "actual.docx"
    target.write_bytes(b"PK\x03\x04 docx payload")
    alias = tmp_path / "misleading.pdf"
    try:
        alias.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    expected = Source(id=SOURCE_ID, title=target.name, status=SourceStatus.READY)
    compat = AsyncMock(return_value=expected)
    api = AndroidSourcesAPI(
        cast(AndroidSession, session),
        pipeline,
        add_file_compat=compat,
    )

    result = await api.add_file(NOTEBOOK_ID, alias)

    assert result is expected
    compat.assert_awaited_once_with(
        NOTEBOOK_ID,
        target.resolve(),
        None,
        wait=False,
        wait_timeout=120.0,
        title=None,
        on_progress=None,
    )
    assert session.calls == []
    assert harness.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["document.pdf", "notes.md", "audio.mp3", "book.epub"])
async def test_public_compat_keeps_every_other_extension_on_android(
    tmp_path: Path,
    filename: str,
) -> None:
    harness = HTTPHarness()
    session, _, pipeline, _ = await _graph(harness)
    path = tmp_path / filename
    path.write_bytes(b"native payload")
    compat = AsyncMock()
    api = AndroidSourcesAPI(
        cast(AndroidSession, session),
        pipeline,
        add_file_compat=compat,
    )

    result = await api.add_file(NOTEBOOK_ID, path)

    assert result.id == SOURCE_ID
    compat.assert_not_awaited()
    assert [call[0] for call in session.calls] == [ADD_TENTATIVE_SOURCES_METHOD]
    assert [call.method for call in harness.calls] == ["POST", "PUT"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("registration_result", "unconfirmed"),
    [
        (_WRITE.AddTentativeSourcesResponse(), False),
        (object(), True),
        (ServerError("safe", rpc_code=14), True),
    ],
)
async def test_registration_failure_never_starts_upload_replays_or_cleans_up(
    tmp_path: Path,
    registration_result: Any,
    unconfirmed: bool,
) -> None:
    harness = HTTPHarness()
    session, bearer, pipeline, api = await _graph(harness)
    session.handlers[ADD_TENTATIVE_SOURCES_METHOD] = registration_result
    path, _ = _write_pdf(tmp_path)

    with pytest.raises(SourceAddError) as raised:
        await api.add_file(NOTEBOOK_ID, path)

    assert getattr(raised.value, "unconfirmed", False) is unconfirmed
    assert raised.value.cause is None
    if unconfirmed:
        assert str(raised.value) == (
            "Android file upload tentative registration outcome is unconfirmed for 'document.pdf'."
        )
        assert cast(Any, raised.value).stage == "register"
        assert "URL add" not in str(raised.value)
    assert [call[0] for call in session.calls] == [ADD_TENTATIVE_SOURCES_METHOD]
    assert bearer.calls == []
    assert harness.calls == []
    assert pipeline._open_files == set()


@pytest.mark.asyncio
async def test_registration_cancellation_propagates_and_closes_descriptor(tmp_path: Path) -> None:
    harness = HTTPHarness()
    session, bearer, pipeline, api = await _graph(harness)
    session.handlers[ADD_TENTATIVE_SOURCES_METHOD] = asyncio.CancelledError()
    path, _ = _write_pdf(tmp_path)

    with pytest.raises(asyncio.CancelledError):
        await api.add_file(NOTEBOOK_ID, path)

    assert bearer.calls == []
    assert harness.calls == []
    assert pipeline._open_files == set()


@pytest.mark.asyncio
async def test_filename_title_is_not_custom_and_performs_zero_project_reads(tmp_path: Path) -> None:
    harness = HTTPHarness()
    session, _, _, api = await _graph(harness)
    path, _ = _write_pdf(tmp_path)

    result = await api.add_file(NOTEBOOK_ID, path, title=f"  {path.name}  ")

    assert result.title == path.name
    assert [call[0] for call in session.calls] == [ADD_TENTATIVE_SOURCES_METHOD]


@pytest.mark.parametrize(
    "url",
    [
        SESSION_URL.replace("https://", "http://"),
        SESSION_URL.replace("notebooklm-pa.googleapis.com", "evil.invalid"),
        SESSION_URL.replace("https://", "https://user:pass@"),
        SESSION_URL + "#fragment",
        SESSION_URL.replace("/upload/upload/", "/upload%2Fupload/"),
        SESSION_URL.replace(NOTEBOOK_ID, SOURCE_ID),
        SESSION_URL + "&unknown=1",
        SESSION_URL + "&upload_id=second",
        SESSION_URL.replace("upload_protocol=resumable", "upload_protocol=other"),
        SESSION_URL.replace("upload_id=session-capability", "upload_id="),
        SESSION_URL.replace("?", "?upload_id=one,"),
        SESSION_URL + "\r\nX-Evil: 1",
    ],
)
def test_session_url_validator_rejects_noncanonical_capabilities(url: str) -> None:
    with pytest.raises(ValidationError, match="Invalid Android upload session"):
        validate_upload_session_url(url, NOTEBOOK_ID)


def test_session_url_validator_preserves_the_original_valid_capability() -> None:
    explicit_port = SESSION_URL.replace(
        "notebooklm-pa.googleapis.com", "notebooklm-pa.googleapis.com:443"
    )
    assert validate_upload_session_url(explicit_port, NOTEBOOK_ID) is explicit_port


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["start", "finalize"])
@pytest.mark.parametrize("status", [302, 401, 403, 500])
async def test_http_failure_never_replays_or_cleans_up_and_only_401_invalidates(
    tmp_path: Path,
    stage: str,
    status: int,
) -> None:
    harness = HTTPHarness()
    if stage == "start":
        harness.start_status = status
    else:
        harness.final_status = status
    session, bearer, _, api = await _graph(harness)
    path, _ = _write_pdf(tmp_path)

    error_type = AuthError if status == 401 else SourceAddError
    with pytest.raises(error_type) as raised:
        await api.add_file(NOTEBOOK_ID, path)

    assert cast(Any, raised.value).source_id == SOURCE_ID
    assert cast(Any, raised.value).stage == stage
    if isinstance(raised.value, SourceAddError):
        assert raised.value.cause is None
    else:
        assert raised.value.__cause__ is None
    assert [call.method for call in harness.calls].count("POST" if stage == "start" else "PUT") == 1
    assert all("Delete" not in call[0] for call in session.calls)
    expected_generation = 1 if stage == "start" else 2
    assert bearer.invalidated == ([expected_generation] if status == 401 else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [("X-Goog-Upload-Status", "active")],
        [("X-Goog-Upload-Status", "active, final"), ("X-Goog-Upload-URL", SESSION_URL)],
        [
            ("X-Goog-Upload-Status", "active"),
            ("X-Goog-Upload-URL", SESSION_URL),
            ("X-Goog-Upload-URL", SESSION_URL),
        ],
        [("X-Goog-Upload-Status", "active"), ("X-Goog-Upload-URL", SESSION_URL + ",x")],
    ],
)
async def test_malformed_start_headers_fail_closed_before_second_bearer(
    tmp_path: Path,
    headers: list[tuple[str, str]],
) -> None:
    harness = HTTPHarness()
    harness.start_headers = headers
    _, bearer, _, api = await _graph(harness)
    path, _ = _write_pdf(tmp_path)

    with pytest.raises(SourceAddError, match="start"):
        await api.add_file(NOTEBOOK_ID, path)

    assert bearer.calls == [7]
    assert [call.method for call in harness.calls] == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("X-Goog-Upload-Status", "active")],
        [
            ("X-Goog-Upload-Status", "final"),
            ("X-Goog-Upload-Status", "final"),
        ],
        [("X-Goog-Upload-Status", "final, active")],
    ],
)
async def test_malformed_finalize_headers_fail_once_and_close_body(
    tmp_path: Path,
    headers: list[tuple[str, str]],
) -> None:
    harness = HTTPHarness()
    harness.final_headers = headers
    _, bearer, pipeline, api = await _graph(harness)
    path, _ = _write_pdf(tmp_path)

    with pytest.raises(SourceAddError) as raised:
        await api.add_file(NOTEBOOK_ID, path)

    assert cast(Any, raised.value).stage == "finalize"
    assert bearer.calls == [7, 7]
    assert [call.method for call in harness.calls] == ["POST", "PUT"]
    assert pipeline._open_files == set()


@pytest.mark.asyncio
async def test_one_aggregate_timeout_closes_client_body_and_dispatches_no_finalize(
    tmp_path: Path,
) -> None:
    harness = HTTPHarness()
    harness.block_post = asyncio.Event()
    # Keep enough budget to reach the deliberately blocked POST even on a
    # loaded Windows xdist worker.  A 20 ms budget could expire during the
    # preceding file-open/registration stages and test the wrong boundary.
    _, bearer, pipeline, api = await _graph(harness, upload_timeout=1.0)
    path, _ = _write_pdf(tmp_path)

    upload = asyncio.create_task(api.add_file(NOTEBOOK_ID, path))
    try:
        # Prove the aggregate deadline is being exercised at the intended
        # start-request boundary before checking its public stage metadata.
        await asyncio.wait_for(harness.post_started.wait(), timeout=2.0)
        with pytest.raises(SourceAddError, match="timed out") as raised:
            await upload
    finally:
        if not upload.done():
            upload.cancel()
        await asyncio.gather(upload, return_exceptions=True)

    assert cast(Any, raised.value).stage == "start"
    assert bearer.calls == [7]
    assert [call.method for call in harness.calls] == ["POST"]
    assert all(client.closed for client in harness.clients)
    assert pipeline._open_files == set()


@pytest.mark.asyncio
async def test_forced_close_cancels_old_body_and_reopen_uses_only_new_epoch(
    tmp_path: Path,
) -> None:
    harness = HTTPHarness()
    harness.block_put = asyncio.Event()
    session, bearer, pipeline, api = await _graph(harness)
    path, _ = _write_pdf(tmp_path)
    old = asyncio.create_task(api.add_file(NOTEBOOK_ID, path))
    await asyncio.wait_for(harness.put_started.wait(), timeout=1.0)

    await pipeline.prepare_close()
    with pytest.raises(RuntimeError, match="transport close"):
        await old
    assert pipeline._open_files == set()
    assert all(client.closed for client in harness.clients)
    assert [call.method for call in harness.calls] == ["POST"]

    session.epoch = 8
    harness.block_put = None
    pipeline.reset_after_open()
    await pipeline.open(asyncio.get_running_loop(), 8)
    reopened = await api.add_file(NOTEBOOK_ID, path)
    assert reopened.id == SOURCE_ID
    assert bearer.calls[-2:] == [8, 8]


@pytest.mark.asyncio
async def test_caller_cancellation_closes_resources_and_sends_no_later_io(tmp_path: Path) -> None:
    harness = HTTPHarness()
    harness.block_put = asyncio.Event()
    _, _, pipeline, api = await _graph(harness)
    path, _ = _write_pdf(tmp_path)
    task = asyncio.create_task(api.add_file(NOTEBOOK_ID, path))
    await asyncio.wait_for(harness.put_started.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert pipeline._open_files == set()
    assert all(client.closed for client in harness.clients)
    assert [call.method for call in harness.calls] == ["POST"]


@pytest.mark.asyncio
async def test_progress_callback_failure_propagates_its_type_and_closes_body(
    tmp_path: Path,
) -> None:
    harness = HTTPHarness()
    _, _, pipeline, api = await _graph(harness)
    path, _ = _write_pdf(tmp_path)

    def fail_progress(sent: int, total: int) -> None:
        del sent, total
        raise ValueError("callback failed")

    with pytest.raises(ValueError, match="callback failed"):
        await api.add_file(NOTEBOOK_ID, path, on_progress=fail_progress)

    assert pipeline._open_files == set()
    assert [call.method for call in harness.calls] == ["POST"]


@pytest.mark.asyncio
async def test_raw_http_runtime_with_session_secret_becomes_bounded_stage_failure(
    tmp_path: Path,
) -> None:
    secret = "raw-http-session-secret"
    harness = HTTPHarness()
    harness.put_error = RuntimeError(f"failed request {SESSION_URL}-{secret}")
    _, _, _, api = await _graph(harness)
    path, _ = _write_pdf(tmp_path)

    with pytest.raises(SourceAddError) as raised:
        await api.add_file(NOTEBOOK_ID, path)

    assert cast(Any, raised.value).stage == "finalize"
    assert raised.value.cause is None
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_hostile_session_capability_never_reaches_bearer_logs_exception_or_traceback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "upload-secret-capability-DO-NOT-LEAK"
    hostile = f"https://evil.invalid/upload/upload/{NOTEBOOK_ID}?upload_id={secret}"
    harness = HTTPHarness()
    harness.start_headers = [
        ("X-Goog-Upload-Status", "active"),
        ("X-Goog-Upload-URL", hostile),
    ]
    session, bearer, pipeline, api = await _graph(harness)
    path, _ = _write_pdf(tmp_path)

    with pytest.raises(SourceAddError) as raised:
        await api.add_file(NOTEBOOK_ID, path)

    error = raised.value
    assert bearer.calls == [7]
    assert error.cause is None
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in str(error)
    assert secret not in repr(error)
    assert secret not in caplog.text
    assert secret not in "".join(traceback.format_exception(error))
    _assert_raw_upload_owners_absent_from_library_traceback(
        error,
        session,
        bearer,
        pipeline,
        api,
    )
    frame = error.__traceback__
    while frame is not None:
        if "/src/notebooklm/" in frame.tb_frame.f_code.co_filename:
            assert secret not in repr(frame.tb_frame.f_locals)
            assert "bearer-secret" not in repr(frame.tb_frame.f_locals)
        frame = frame.tb_next


@pytest.mark.asyncio
async def test_drive_staging_auth_error_public_traceback_retains_no_bearer_owner(
    tmp_path: Path,
) -> None:
    harness = HTTPHarness()
    harness.drive_stage_status = 401
    _, bearer, pipeline, api = await _graph(harness)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")

    with pytest.raises(AuthError) as captured:
        await api.add_file(NOTEBOOK_ID, path)

    error = captured.value
    assert bearer.invalidated == [1]
    assert error.__cause__ is None
    assert error.__context__ is None
    frame = error.__traceback__
    while frame is not None:
        if "/src/notebooklm/" in frame.tb_frame.f_code.co_filename:
            values = tuple(frame.tb_frame.f_locals.values())
            assert api not in values
            assert pipeline not in values
            assert bearer not in values
            assert not any(getattr(value, "_bearer_provider", None) is bearer for value in values)
        frame = frame.tb_next


@pytest.mark.asyncio
async def test_docx_stages_through_drive_and_removes_the_staged_copy(tmp_path: Path) -> None:
    """No Web collaborator: ``.docx`` round-trips through the caller's Drive.

    The mobile upload frontend parses no Word, but the mobile backend does, so
    the file is staged in Drive, imported, and the staged copy deleted.
    """
    harness = HTTPHarness()
    session, _, pipeline, api = await _graph(harness)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")

    result = await api.add_file(NOTEBOOK_ID, path, wait=True, wait_timeout=30.0)

    assert result.id == SOURCE_ID

    stage = next(c for c in harness.calls if c.url.startswith(DRIVE_STAGING_UPLOAD_URL))
    assert "uploadType=multipart" in stage.url
    assert stage.headers["Authorization"].startswith("Bearer ")
    assert b"wordprocessingml.document" in stage.body
    assert b"PK\x03\x04 docx payload" in stage.body

    # Imported by reference; the bytes never touch the mobile Scotty frontend.
    assert not any(c.url.startswith(UPLOAD_ORIGIN) for c in harness.calls)
    assert ADD_SOURCES_METHOD in [call[0] for call in session.calls]
    drive_content = next(
        call[1].user_content[0].google_drive_content
        for call in session.calls
        if call[0] == ADD_SOURCES_METHOD
    )
    assert drive_content.document_id == DRIVE_STAGED_ID

    deletes = [c for c in harness.calls if c.method == "DELETE"]
    assert len(deletes) == 1
    assert DRIVE_STAGED_ID in deletes[0].url
    del pipeline


@pytest.mark.asyncio
async def test_drive_staged_copy_is_removed_when_registration_fails(tmp_path: Path) -> None:
    """Staging happens before registration, so its failure must still clean up."""
    harness = HTTPHarness()
    session, _, pipeline, api = await _graph(harness)
    session.handlers[ADD_TENTATIVE_SOURCES_METHOD] = ServerError("rejected", rpc_code=13)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")

    with pytest.raises(SourceAddError):
        await api.add_file(NOTEBOOK_ID, path, wait=True, wait_timeout=30.0)

    deletes = [c for c in harness.calls if c.method == "DELETE"]
    assert len(deletes) == 1
    assert DRIVE_STAGED_ID in deletes[0].url
    del pipeline


@pytest.mark.asyncio
async def test_drive_unstage_failure_does_not_mask_a_successful_add(tmp_path: Path) -> None:
    """An orphaned staging file is untidy, not a failed add."""
    harness = HTTPHarness()
    harness.drive_delete_error = httpx.ConnectError("drive unreachable")
    _, _, pipeline, api = await _graph(harness)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")

    result = await api.add_file(NOTEBOOK_ID, path, wait=True, wait_timeout=30.0)

    assert result.id == SOURCE_ID
    del pipeline


@pytest.mark.asyncio
async def test_drive_staging_rejects_a_file_over_the_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = HTTPHarness()
    _, _, pipeline, api = await _graph(harness)
    path = tmp_path / "huge.docx"
    path.write_bytes(b"x")

    with monkeypatch.context() as patched:
        patched.setattr(drive_staging_module, "_MAX_DRIVE_STAGING_BYTES", 0)
        with pytest.raises(ValidationError, match="Drive staging is capped"):
            await api.add_file(NOTEBOOK_ID, path, wait=True, wait_timeout=30.0)

    # Nothing was staged, so nothing needs deleting.
    assert not any(c.method == "DELETE" for c in harness.calls)
    del pipeline


def test_every_supported_extension_is_classified_exactly_once() -> None:
    """A new upload extension must be classified, not silently defaulted.

    ``add_file`` picks its transport from ``_DRIVE_STAGED_UPLOAD_EXTENSIONS``
    and falls through to the native Scotty transaction otherwise. Without this
    gate, adding a file type to the public set quietly routes it at the mobile
    frontend, which parses only a narrow allowlist -- exactly how ``.pptx``
    was missed when the Drive set was first written with ``.docx`` alone.
    """
    arms = (_NATIVE_UPLOAD_EXTENSIONS, _DRIVE_STAGED_UPLOAD_EXTENSIONS)

    union: set[str] = set()
    for arm in arms:
        assert not (union & arm), f"extension classified twice: {sorted(union & arm)}"
        union |= arm

    assert union == set(_UPLOAD_FILE_EXTENSIONS), (
        "unclassified upload extension(s): "
        f"{sorted(set(_UPLOAD_FILE_EXTENSIONS) ^ union)}. Live-probe the file on "
        "both backends, then add it to exactly one arm."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", sorted(_DRIVE_STAGED_UPLOAD_EXTENSIONS))
async def test_every_drive_staged_extension_routes_through_drive(
    tmp_path: Path,
    extension: str,
) -> None:
    harness = HTTPHarness()
    _, _, pipeline, api = await _graph(harness)
    path = tmp_path / f"deck{extension}"
    path.write_bytes(b"PK\x03\x04 ooxml payload")

    result = await api.add_file(NOTEBOOK_ID, path, wait=True, wait_timeout=30.0)

    assert result.id == SOURCE_ID
    assert any(c.url.startswith(DRIVE_STAGING_UPLOAD_URL) for c in harness.calls)
    assert not any(c.url.startswith(UPLOAD_ORIGIN) for c in harness.calls)
    assert [c for c in harness.calls if c.method == "DELETE"]
    del pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", sorted(_NATIVE_UPLOAD_EXTENSIONS))
async def test_every_native_extension_skips_drive(tmp_path: Path, extension: str) -> None:
    harness = HTTPHarness()
    _, _, pipeline, api = await _graph(harness)
    path = tmp_path / f"sample{extension}"
    path.write_bytes(b"native payload")

    result = await api.add_file(NOTEBOOK_ID, path, wait=True, wait_timeout=30.0)

    assert result.id == SOURCE_ID
    assert not any(c.url.startswith(DRIVE_STAGING_UPLOAD_URL) for c in harness.calls)
    assert any(c.url.startswith(UPLOAD_ORIGIN) for c in harness.calls)
    del pipeline


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        (".csv", "text/csv"),
        (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        (".epub", "application/epub+zip"),
        (".md", "text/markdown"),
        (".pdf", "application/pdf"),
        (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        (".txt", "text/plain"),
    ],
)
def test_supported_extension_mime_does_not_depend_on_the_platform(
    filename: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mimetypes`` reads the Windows registry and returns nothing for OOXML.

    That fell through to ``application/octet-stream``, which on the Drive-staged
    path becomes the staged file's declared type and decides how the backend
    parses it. Simulate the barren registry and require the pinned answer.
    """
    monkeypatch.setattr(upload_module.mimetypes, "guess_type", lambda *_a, **_k: (None, None))
    assert _resolve_upload_content_type(Path(f"sample{filename}"), None) == expected


@pytest.mark.asyncio
async def test_blank_title_is_rejected_before_any_drive_io(tmp_path: Path) -> None:
    harness = HTTPHarness()
    _, _, pipeline, api = await _graph(harness)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")

    with pytest.raises(ValidationError, match="Title cannot be empty"):
        await api.add_file(NOTEBOOK_ID, path, title="   ", wait=True, wait_timeout=30.0)

    assert harness.calls == []
    del pipeline


@pytest.mark.asyncio
async def test_html_mime_is_rejected_before_any_drive_io(tmp_path: Path) -> None:
    """The Drive path enforces the same MIME policy as the native uploader."""
    harness = HTTPHarness()
    _, _, pipeline, api = await _graph(harness)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")

    with pytest.raises(ValidationError, match="HTML file uploads are not supported"):
        await api.add_file(NOTEBOOK_ID, path, "text/html", wait=True, wait_timeout=30.0)

    assert harness.calls == []
    del pipeline


@pytest.mark.asyncio
async def test_staging_transport_failure_surfaces_as_network_error(tmp_path: Path) -> None:
    """Retry-by-public-exception-type must work here as on every other transfer."""
    harness = HTTPHarness()
    harness.drive_stage_error = httpx.ConnectError("drive unreachable")
    _, _, pipeline, api = await _graph(harness)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")

    with pytest.raises(NetworkError):
        await api.add_file(NOTEBOOK_ID, path, wait=True, wait_timeout=30.0)

    assert not any(c.method == "DELETE" for c in harness.calls)
    del pipeline


@pytest.mark.asyncio
async def test_cap_is_enforced_on_the_bytes_actually_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file that grows between stat() and read() must not bypass the cap."""
    harness = HTTPHarness()
    _, _, pipeline, api = await _graph(harness)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")

    real_fstat = drive_staging_module.os.fstat

    class _UndersizedStat:
        """fstat reports a compliant size; the descriptor holds more."""

        def __init__(self, real: Any) -> None:
            self._real = real

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

        @property
        def st_size(self) -> int:
            return 1

    path.write_bytes(b"x" * 4096)
    with monkeypatch.context() as patched:
        patched.setattr(drive_staging_module, "_MAX_DRIVE_STAGING_BYTES", 8)
        patched.setattr(
            drive_staging_module.os,
            "fstat",
            lambda fd: _UndersizedStat(real_fstat(fd)),
        )
        with pytest.raises(ValidationError, match="Drive staging is capped"):
            await api.add_file(NOTEBOOK_ID, path, wait=True, wait_timeout=30.0)

    assert not any(c.url.startswith(DRIVE_STAGING_UPLOAD_URL) for c in harness.calls)
    del pipeline


@pytest.mark.asyncio
async def test_staged_file_is_kept_when_the_import_does_not_settle(tmp_path: Path) -> None:
    """A timeout may mean the import is still reading the file.

    Deleting the only copy then can turn a slow-but-successful import into a
    permanently errored source, so the file is retained and named.
    """
    harness = HTTPHarness()
    session, _, pipeline, api = await _graph(harness)
    session.handlers[GET_PROJECT_METHOD] = SourceTimeoutError("src", timeout=1.0)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")

    with pytest.raises(SourceTimeoutError):
        await api.add_file(NOTEBOOK_ID, path, wait=True, wait_timeout=30.0)

    assert any(c.url.startswith(DRIVE_STAGING_UPLOAD_URL) for c in harness.calls)
    assert not any(c.method == "DELETE" for c in harness.calls)
    del pipeline


@pytest.mark.asyncio
async def test_a_refused_cleanup_delete_is_warned_not_silently_accepted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-2xx DELETE returns normally; without a status check it looked clean."""
    harness = HTTPHarness()
    harness.drive_delete_status = 403
    _, _, pipeline, api = await _graph(harness)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")

    with caplog.at_level("WARNING", logger="notebooklm._android.drive_staging"):
        result = await api.add_file(NOTEBOOK_ID, path, wait=True, wait_timeout=30.0)

    assert result.id == SOURCE_ID  # the add still succeeded
    assert DRIVE_STAGED_ID in caplog.text
    assert "HTTP 403" in caplog.text
    del pipeline


def test_curl_transport_implements_delete_for_staging_cleanup() -> None:
    """Without it the cleanup AttributeErrors and silently leaks the staged file."""
    assert callable(CurlCffiAsyncClient.delete)


@pytest.mark.asyncio
async def test_a_non_regular_file_is_rejected_before_the_staged_read(
    tmp_path: Path,
) -> None:
    """A FIFO reports size zero, then blocks ``read_bytes`` in a worker thread.

    Nothing bounds that read and cancellation cannot stop it, so it has to be
    refused up front — the same guard the native uploader applies.
    """
    harness = HTTPHarness()
    _, _, pipeline, api = await _graph(harness)
    fifo = tmp_path / "report.docx"
    try:
        os.mkfifo(fifo)
    except (AttributeError, NotImplementedError, OSError) as error:
        pytest.skip(f"FIFOs unavailable: {error}")

    with pytest.raises(ValidationError, match="Not a regular file"):
        await api.add_file(NOTEBOOK_ID, fifo, wait=True, wait_timeout=30.0)

    assert harness.calls == []
    del pipeline


@pytest.mark.asyncio
async def test_drive_staged_title_is_trimmed_like_the_native_path(tmp_path: Path) -> None:
    """`" report "` must not title differently depending on the upload route."""
    harness = HTTPHarness()
    session, _, pipeline, api = await _graph(harness)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")

    await api.add_file(NOTEBOOK_ID, path, title="  Quarterly report  ", wait=True)

    drive_content = next(
        call[1].user_content[0].google_drive_content
        for call in session.calls
        if call[0] == ADD_SOURCES_METHOD
    )
    assert drive_content.source_name == "Quarterly report"
    del pipeline


@pytest.mark.asyncio
async def test_drive_staged_copy_is_removed_when_the_import_itself_fails(
    tmp_path: Path,
) -> None:
    """A settled *import* failure also cleans up.

    Distinct from the registration case above: here the source is registered
    and committed, and the backend then reports it errored. That is a settled
    outcome — unlike a timeout — so the staged copy is dead weight and goes.
    """
    harness = HTTPHarness()
    session, _, pipeline, api = await _graph(harness)
    session.handlers[GET_PROJECT_METHOD] = _project(_SETTINGS.SOURCE_STATUS_ERROR)
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")

    with pytest.raises(SourceProcessingError):
        await api.add_file(NOTEBOOK_ID, path, wait=True, wait_timeout=30.0)

    assert any(c.url.startswith(DRIVE_STAGING_UPLOAD_URL) for c in harness.calls)
    deletes = [c for c in harness.calls if c.method == "DELETE"]
    assert len(deletes) == 1
    assert DRIVE_STAGED_ID in deletes[0].url
    del pipeline
