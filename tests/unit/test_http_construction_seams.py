"""Transfer construction follows its assembled owner without global patches."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm._deadline import RuntimeDeadline
from notebooklm._http_client_factory import HttpClientFactories
from notebooklm.exceptions import ArtifactDownloadError
from tests._fault_server.android import SyntheticMasterTokenReader, SyntheticOAuthMinter
from tests._fault_server.web import synthetic_auth
from tests._helpers.client_factory import build_client_shell_for_tests

PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-image"
NOTEBOOK = "00000000-0000-4000-8000-000000000200"
SOURCE = "00000000-0000-4000-8000-000000000201"


class CapturedHTTP:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.clients: list[httpx.AsyncClient] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "unmapped.google.com":
            raise httpx.ConnectError("unmapped destination", request=request)
        if request.headers.get("x-goog-upload-command") == "start":
            return httpx.Response(
                200,
                headers={
                    "x-goog-upload-status": "active",
                    "x-goog-upload-url": str(request.url.copy_with(query=b"upload_id=opaque")),
                },
            )
        if request.headers.get("x-goog-upload-command") == "upload, finalize":
            return httpx.Response(200, headers={"x-goog-upload-status": "final"})
        if request.method == "POST" and request.url.path == "/upload/drive/v3/files":
            return httpx.Response(200, json={"id": "staged-opaque"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://untrusted.example/asset"})
        if request.url.host == "drive.usercontent.google.com":
            return httpx.Response(
                200,
                content=b"drive text",
                headers={
                    "content-type": "text/plain",
                    "content-disposition": 'attachment; filename="fixture.txt"',
                },
            )
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    def factory(self, **kwargs: Any) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle), trust_env=False, **kwargs
        )
        self.clients.append(client)
        return client


def shell(backend: str, capture: CapturedHTTP) -> Any:
    return build_client_shell_for_tests(
        synthetic_auth(),
        backend=backend,
        async_client_factory=capture.factory,
        http_client_factories=HttpClientFactories(httpx=capture.factory),
        master_token_reader=SyntheticMasterTokenReader(),
        oauth_minter=SyntheticOAuthMinter(),
    )


@pytest.mark.parametrize("backend", ["web", "android"])
@pytest.mark.parametrize("batch", [False, True])
async def test_assembled_asset_factory_reaches_single_and_batch(
    backend: str, batch: bool, tmp_path: Path
) -> None:
    capture = CapturedHTTP()
    client = shell(backend, capture)
    destination = tmp_path / "asset.png"
    url = "https://lh3.googleusercontent.com/asset"
    async with client:
        assets = client.artifacts._asset_downloads
        if batch:
            result = await assets.download_urls_batch([(url, str(destination))])
            assert result.succeeded == [str(destination)]
            assert result.failed == []
        else:
            assert await assets.download_url(url, str(destination)) == str(destination)
        assert destination.read_bytes() == PNG
    assert len(capture.requests) == 1
    assert all(item.is_closed for item in capture.clients)
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("backend", ["web", "android"])
async def test_injected_asset_factory_keeps_redirect_validation(
    backend: str, tmp_path: Path
) -> None:
    capture = CapturedHTTP()
    async with shell(backend, capture) as client:
        with pytest.raises(ArtifactDownloadError):
            await client.artifacts._asset_downloads.download_url(
                "https://lh3.googleusercontent.com/redirect", str(tmp_path / "asset.png")
            )
    assert len(capture.requests) == 1
    assert capture.requests[0].url.host == "lh3.googleusercontent.com"
    assert list(tmp_path.iterdir()) == []


async def test_web_upload_start_finalize_and_drive_capture_factories(tmp_path: Path) -> None:
    capture = CapturedHTTP()
    async with shell("web", capture) as client:
        uploader = client._web_runtime.source_uploader
        async with uploader.transport_operation_scope("test-upload") as epoch:
            session = await uploader.start_resumable_upload(
                NOTEBOOK, "fixture.txt", 4, SOURCE, "text/plain", expected_epoch=epoch
            )
            handle = io.BytesIO(b"body")
            await uploader.upload_file_streaming(
                session, handle, total_bytes=4, expected_epoch=epoch
            )
            assert handle.closed
        async with uploader.drive_download_scope("opaque_drive_file_id") as (path, name, mime):
            assert path.read_bytes() == b"drive text"
            assert name == "fixture.txt"
        assert not path.exists()
    assert len(capture.requests) == 3
    assert capture.requests[1].content == b"body"
    assert all(item.is_closed for item in capture.clients)


async def test_android_upload_and_drive_capture_factories(tmp_path: Path) -> None:
    capture = CapturedHTTP()
    source = tmp_path / "fixture.csv"
    source.write_text("one,two\n")
    async with shell("android", capture) as client:
        uploader = client._android_runtime.upload_pipeline
        epoch = uploader._active_epoch
        deadline = RuntimeDeadline.start(5)
        start = await uploader._start_worker(
            NOTEBOOK, SOURCE, "fixture.txt", 4, "text/plain", epoch, deadline
        )
        assert start.upload_status == "active"
        handle = io.BytesIO(b"body")
        try:
            final = await uploader._finalize_worker(
                start.session_url, handle, 4, None, epoch, deadline
            )
        finally:
            handle.close()
        assert final.upload_status == "final"
        staging = uploader._drive_staging()
        file_id = await staging.stage(source, source.name, "text/csv")
        assert file_id == "staged-opaque"
        await staging.unstage(file_id)
    assert [item.method for item in capture.requests] == ["POST", "PUT", "POST", "DELETE"]
    assert all(item.is_closed for item in capture.clients)


async def test_concurrent_web_owners_keep_factories_and_fail_closed(tmp_path: Path) -> None:
    first, second = CapturedHTTP(), CapturedHTTP()
    async with shell("web", first) as a, shell("web", second) as b:
        await asyncio.gather(
            a.artifacts._asset_downloads.download_url(
                "https://lh3.googleusercontent.com/one", str(tmp_path / "one.png")
            ),
            b.artifacts._asset_downloads.download_url(
                "https://lh3.googleusercontent.com/two", str(tmp_path / "two.png")
            ),
        )
        with pytest.raises(ArtifactDownloadError):
            await a.artifacts._asset_downloads.download_url(
                "https://unmapped.google.com/asset", str(tmp_path / "missing.png")
            )
    assert [r.url.path for r in first.requests] == ["/one", "/asset"]
    assert [r.url.path for r in second.requests] == ["/two"]
    assert not (tmp_path / "missing.png").exists()
