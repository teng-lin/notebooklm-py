"""Android-bearer Drive download contract for ``sources.add_drive_file``."""

from __future__ import annotations

import asyncio
import stat
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from notebooklm._android import upload as android_upload
from notebooklm._android.auth import BearerCredential, BearerProvider
from notebooklm._android.session import AndroidSession
from notebooklm._android.upload import AndroidUploadPipeline
from notebooklm._source.drive import parse_drive_ref
from notebooklm.exceptions import AuthError, NetworkError, ValidationError


@dataclass(frozen=True)
class _Lease:
    epoch: int


class FakeSession:
    def __init__(self) -> None:
        self.epoch = 7
        self.scopes: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        self.scopes.append(label)
        yield _Lease(self.epoch)

    def assert_epoch(self, expected_epoch: int) -> None:
        if expected_epoch != self.epoch:
            raise RuntimeError("retired fake epoch")


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


async def _pipeline(
    handler: Any,
) -> tuple[FakeSession, FakeBearerProvider, AndroidUploadPipeline]:
    session = FakeSession()
    bearer = FakeBearerProvider()
    transport = httpx.MockTransport(handler)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    pipeline = AndroidUploadPipeline(
        session=cast(AndroidSession, session),
        bearer_provider=cast(BearerProvider, bearer),
        upload_timeout=2.0,
        async_client_factory=factory,
    )
    loop = asyncio.get_running_loop()
    pipeline.set_bound_loop(loop)
    pipeline.reset_after_open()
    await pipeline.open(loop, session.epoch)
    return session, bearer, pipeline


class _HangingDriveResponse:
    def __init__(self, body_started: asyncio.Event | None = None) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self._body_started = body_started
        self.aborts = 0

    def abort(self) -> None:
        self.aborts += 1

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        assert self._body_started is not None
        self._body_started.set()
        await asyncio.Event().wait()
        yield b""  # pragma: no cover - the wait is deliberately never released


class _HangingDriveStream:
    def __init__(self, *, hang_entry: bool, block_exit: bool = False) -> None:
        self.hang_entry = hang_entry
        self.entry_started = asyncio.Event()
        self.body_started = asyncio.Event()
        self.exit_started = asyncio.Event()
        self.exit_release = asyncio.Event()
        if not block_exit:
            self.exit_release.set()
        self.enter_cancelled = False
        self.exits = 0
        self.exit_args: tuple[object, ...] | None = None
        self.response = _HangingDriveResponse(self.body_started)

    async def __aenter__(self) -> _HangingDriveResponse:
        self.entry_started.set()
        if self.hang_entry:
            try:
                await asyncio.Event().wait()
            finally:
                self.enter_cancelled = True
        return self.response

    async def __aexit__(self, *exc: object) -> None:
        self.exits += 1
        self.exit_args = exc
        self.exit_started.set()
        await self.exit_release.wait()


class _HangingDriveClient:
    def __init__(self, stream: _HangingDriveStream) -> None:
        self._stream = stream
        self.closed = False

    async def __aenter__(self) -> _HangingDriveClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self.closed = True

    async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "abcdefghijklmnopqrstuvwxyz123456",
                "name": "notes.txt",
                "mimeType": "text/plain",
                "size": "10",
                "capabilities": {"canDownload": True},
            },
            request=httpx.Request("GET", url),
        )

    def stream(self, *_args: Any, **_kwargs: Any) -> _HangingDriveStream:
        return self._stream


async def _hanging_pipeline(
    stream: _HangingDriveStream,
) -> tuple[AndroidUploadPipeline, _HangingDriveClient]:
    session = FakeSession()
    bearer = FakeBearerProvider()
    client = _HangingDriveClient(stream)
    pipeline = AndroidUploadPipeline(
        session=cast(AndroidSession, session),
        bearer_provider=cast(BearerProvider, bearer),
        upload_timeout=httpx.Timeout(None),
        async_client_factory=lambda **_kwargs: client,  # type: ignore[arg-type]
    )
    loop = asyncio.get_running_loop()
    pipeline.set_bound_loop(loop)
    pipeline.reset_after_open()
    await pipeline.open(loop, session.epoch)
    return pipeline, client


@pytest.mark.asyncio
async def test_drive_download_uses_android_bearer_resource_key_and_cleans_exact_temp() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer bearer-secret-1"
        assert request.headers["x-goog-drive-resource-keys"] == (
            "abcdefghijklmnopqrstuvwxyz123456/resource-secret"
        )
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=b"drive text", request=request)
        return httpx.Response(
            200,
            json={
                "id": "abcdefghijklmnopqrstuvwxyz123456",
                "name": "folder\\notes.txt",
                "mimeType": "text/plain",
                "size": "10",
                "capabilities": {"canDownload": True},
            },
            request=request,
        )

    session, bearer, pipeline = await _pipeline(handler)
    parent: Path | None = None
    async with pipeline.drive_download_scope(
        "https://drive.google.com/file/d/abcdefghijklmnopqrstuvwxyz123456/view"
        "?resourcekey=resource-secret"
    ) as (path, filename, content_type):
        parent = path.parent
        assert path.name == "notes.txt"
        assert filename == "notes.txt"
        assert content_type == "text/plain"
        assert path.read_bytes() == b"drive text"
        if sys.platform == "win32":
            # Windows inherits NTFS ACLs from the user's temp directory;
            # Python 3.12 stat exposes synthetic 0666/0777 POSIX bits that do
            # not describe those ACLs.
            assert path.is_file()
            assert path.parent.is_dir()
        else:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert parent is not None and not parent.exists()
    assert bearer.calls == [session.epoch]
    assert bearer.invalidated == []
    assert session.scopes == ["sources.add_drive_file.download"]
    assert len(calls) == 2
    assert calls[0].url.params["fields"] == "id,name,mimeType,size,capabilities(canDownload)"
    assert calls[1].url.params["alt"] == "media"


@pytest.mark.asyncio
async def test_unbounded_httpx_timeout_cannot_bypass_aggregate_stream_entry_deadline() -> None:
    stream = _HangingDriveStream(hang_entry=True)
    pipeline, client = await _hanging_pipeline(stream)
    # Keep the production resolution assertion above the short test-only
    # budget: httpx has no component timers, while the aggregate fence remains
    # independently finite (300 seconds in production).
    assert pipeline._http_timeout == httpx.Timeout(None)
    assert pipeline._upload_timeout == 300.0
    # Give the metadata and temporary-file stages enough time to reach stream
    # entry on a loaded Windows xdist worker.  The old 10 ms budget could expire
    # before ``__aenter__`` was scheduled, so it tested the aggregate pre-entry
    # fence instead of the deliberately stalled header wait below.
    pipeline._upload_timeout = 1.0

    async def download() -> None:
        async with pipeline.drive_download_scope("abcdefghijklmnopqrstuvwxyz123456"):
            pytest.fail("a stream whose header wait hangs must not yield")

    task = asyncio.create_task(download())
    try:
        await asyncio.wait_for(stream.entry_started.wait(), timeout=2.0)
        with pytest.raises(NetworkError, match="TimeoutError"):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert stream.entry_started.is_set()
    assert stream.enter_cancelled
    # Async-context protocol does not call __aexit__ when __aenter__ fails;
    # cancellation of the entry coroutine owns that partial cleanup.
    assert stream.exits == 0
    assert client.closed
    assert pipeline._open_files == set()
    assert pipeline._transport_clients == set()


@pytest.mark.asyncio
async def test_drive_stream_exit_settles_before_repeated_cancellation_escapes() -> None:
    stream = _HangingDriveStream(hang_entry=False, block_exit=True)
    pipeline, client = await _hanging_pipeline(stream)

    async def download() -> None:
        async with pipeline.drive_download_scope("abcdefghijklmnopqrstuvwxyz123456"):
            pytest.fail("a cancelled media body must not yield")

    task = asyncio.create_task(download())
    await asyncio.wait_for(stream.body_started.wait(), timeout=1.0)
    task.cancel()
    await asyncio.wait_for(stream.exit_started.wait(), timeout=1.0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    stream.exit_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.exits == 1
    assert stream.response.aborts == 1
    assert stream.exit_args is not None
    assert stream.exit_args[0] is asyncio.CancelledError
    assert isinstance(stream.exit_args[1], asyncio.CancelledError)
    assert client.closed
    assert pipeline._open_files == set()
    assert pipeline._transport_clients == set()


def test_drive_temp_permissions_preserve_windows_acl_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(android_upload.sys, "platform", "win32")
    monkeypatch.setattr(
        android_upload.os,
        "chmod",
        lambda *_args: pytest.fail("Windows temporary files must inherit ACLs"),
    )

    android_upload._set_private_temp_permissions(Path("unused"), 0o600)
    android_upload._set_private_temp_permissions(Path("unused-directory"), 0o700)


@pytest.mark.asyncio
async def test_drive_download_rejects_native_document_before_media_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "id": "abcdefghijklmnopqrstuvwxyz123456",
                "name": "Native document",
                "mimeType": "application/vnd.google-apps.document",
                "capabilities": {"canDownload": True},
            },
            request=request,
        )

    _, _, pipeline = await _pipeline(handler)
    with pytest.raises(ValidationError, match="sources.add_drive"):
        async with pipeline.drive_download_scope("abcdefghijklmnopqrstuvwxyz123456"):
            pytest.fail("native Drive documents must not yield a temp file")
    assert calls == 1


@pytest.mark.asyncio
async def test_drive_download_invalidates_rejected_bearer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    _, bearer, pipeline = await _pipeline(handler)
    with pytest.raises(AuthError):
        async with pipeline.drive_download_scope("abcdefghijklmnopqrstuvwxyz123456"):
            pytest.fail("rejected bearer must not yield a temp file")
    assert bearer.invalidated == [1]


@pytest.mark.asyncio
async def test_drive_download_transport_failure_does_not_retain_bearer_request() -> None:
    resource_key = "resource-secret-must-not-escape"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer bearer-secret-1"
        assert resource_key in request.headers["x-goog-drive-resource-keys"]
        raise httpx.ConnectError("connection failed", request=request)

    _, _, pipeline = await _pipeline(handler)
    with pytest.raises(NetworkError) as caught:
        async with pipeline.drive_download_scope(
            "https://drive.google.com/file/d/abcdefghijklmnopqrstuvwxyz123456/view"
            f"?resourcekey={resource_key}"
        ):
            pytest.fail("failed Drive request must not yield a temp file")

    assert caught.value.original_error is None
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "bearer-secret" not in repr(caught.value.__dict__)
    traceback = caught.value.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if "/src/notebooklm/" in frame.f_code.co_filename:
            assert {
                "bearer",
                "client",
                "credential",
                "document_id",
                "error",
                "headers",
                "pipeline",
                "ref",
                "request",
                "self",
            }.isdisjoint(frame.f_locals)
            assert all(
                "bearer-secret" not in value and resource_key not in value
                for value in frame.f_locals.values()
                if isinstance(value, str)
            )
        traceback = traceback.tb_next


@pytest.mark.asyncio
async def test_drive_download_does_not_reclassify_consumer_oserror() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=b"drive text", request=request)
        return httpx.Response(
            200,
            json={
                "id": "abcdefghijklmnopqrstuvwxyz123456",
                "name": "notes.txt",
                "mimeType": "text/plain",
                "size": "10",
            },
            request=request,
        )

    _, _, pipeline = await _pipeline(handler)
    parent: Path | None = None
    with pytest.raises(OSError, match="consumer failed") as caught:
        async with pipeline.drive_download_scope("abcdefghijklmnopqrstuvwxyz123456") as result:
            parent = result[0].parent
            raise OSError("consumer failed")

    assert type(caught.value) is OSError
    assert parent is not None and not parent.exists()


def test_drive_parser_never_echoes_rejected_resource_key() -> None:
    secret = "resource-key-must-not-escape"
    with pytest.raises(ValidationError) as caught:
        parse_drive_ref(
            f"https://evil.invalid/file/d/abcdefghijklmnopqrstuvwxyz123456?resourcekey={secret}"
        )
    assert secret not in str(caught.value)
