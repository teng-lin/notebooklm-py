"""Unit tests for the private source upload pipeline."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from notebooklm._source_upload import SourceUploadPipeline, _extract_register_file_source_id
from notebooklm.rpc import RPCError, RPCMethod
from notebooklm.types import Source, SourceAddError


class UploadRuntime:
    def __init__(self) -> None:
        self.semaphore = asyncio.Semaphore(1)
        self.queue_waits: list[float] = []
        self.labels: list[str] = []
        self.finished: list[object] = []

    def get_upload_semaphore(self) -> asyncio.Semaphore:
        return self.semaphore

    def record_upload_queue_wait(self, wait_seconds: float) -> None:
        self.queue_waits.append(wait_seconds)

    async def begin_transport_post(self, log_label: str) -> object:
        token = object()
        self.labels.append(log_label)
        return token

    async def begin_transport_task(
        self,
        task: asyncio.Task[Any],
        log_label: str,
    ) -> object:
        self.labels.append(log_label)
        return object()

    async def finish_transport_post(self, token: object) -> None:
        self.finished.append(token)


class HttpRuntime:
    def __init__(self) -> None:
        self.cookies = httpx.Cookies()

    @property
    def authuser(self) -> int:
        return 0

    @property
    def account_email(self) -> str | None:
        return None

    def authuser_query(self) -> str:
        return "authuser=0"

    def authuser_header(self) -> str:
        return "0"

    def live_cookies(self) -> httpx.Cookies:
        return self.cookies


class RecordingRpc:
    def __init__(self, response: Any | BaseException) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
    ) -> Any:
        self.calls.append(
            {
                "method": method,
                "params": params,
                "source_path": source_path,
                "allow_null": allow_null,
                "disable_internal_retries": disable_internal_retries,
            }
        )
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


@pytest.fixture
def service() -> SourceUploadPipeline:
    return SourceUploadPipeline()


def test_extract_register_file_source_id_skips_large_string_candidates() -> None:
    long_payload = " " + ("x" * 2000) + " "

    assert _extract_register_file_source_id([long_payload, "src_123"], "report.pdf") == "src_123"


@pytest.mark.asyncio
async def test_add_file_uses_late_bound_hooks_and_finishes_transport(
    service: SourceUploadPipeline,
    tmp_path,
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"hello")
    runtime = UploadRuntime()

    register_file_source = AsyncMock(return_value="src_123")
    start_resumable_upload = AsyncMock(return_value="https://upload.example.com/session")

    async def upload_file_streaming(upload_url, file_obj, **kwargs):
        assert upload_url == "https://upload.example.com/session"
        assert file_obj.read() == b"hello"
        assert kwargs["filename"] == "report.pdf"
        assert kwargs["total_bytes"] == 5
        file_obj.close()

    source = await service.add_file(
        "nb_123",
        file_path,
        capabilities=runtime,
        register_file_source=register_file_source,
        start_resumable_upload=start_resumable_upload,
        upload_file_streaming=upload_file_streaming,
        wait_until_ready=AsyncMock(),
        wait_until_registered=AsyncMock(),
        rename=AsyncMock(),
        logger=MagicMock(),
    )

    assert source.id == "src_123"
    assert source.title == "report.pdf"
    assert source.is_processing
    assert runtime.labels == ["source upload report.pdf"]
    assert len(runtime.finished) == 1
    register_file_source.assert_awaited_once_with("nb_123", "report.pdf")
    start_resumable_upload.assert_awaited_once_with("nb_123", "report.pdf", 5, "src_123")


@pytest.mark.asyncio
async def test_add_file_custom_title_waits_for_registration_before_rename(
    service: SourceUploadPipeline,
    tmp_path,
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"hello")
    runtime = UploadRuntime()
    registered = Source(id="src_123", title="report.pdf", _type_code=7, url="https://source")
    renamed = Source(id="src_123", title="Custom")
    wait_until_registered = AsyncMock(return_value=registered)
    rename = AsyncMock(return_value=renamed)

    async def upload_file_streaming(_upload_url, file_obj, **_kwargs):
        file_obj.close()

    source = await service.add_file(
        "nb_123",
        file_path,
        title="  Custom  ",
        wait_timeout=45.0,
        capabilities=runtime,
        register_file_source=AsyncMock(return_value="src_123"),
        start_resumable_upload=AsyncMock(return_value="https://upload.example.com/session"),
        upload_file_streaming=upload_file_streaming,
        wait_until_ready=AsyncMock(),
        wait_until_registered=wait_until_registered,
        rename=rename,
        logger=MagicMock(),
    )

    assert source == Source(id="src_123", title="Custom", _type_code=7, url="https://source")
    wait_until_registered.assert_awaited_once_with("nb_123", "src_123", timeout=45.0)
    rename.assert_awaited_once_with("nb_123", "src_123", "Custom")


@pytest.mark.asyncio
async def test_register_file_source_uses_rpc_shape_and_wraps_rpc_error(
    service: SourceUploadPipeline,
) -> None:
    rpc_error = RPCError("bad response")
    rpc = RecordingRpc(rpc_error)

    with pytest.raises(SourceAddError) as exc_info:
        await service.register_file_source("nb_123", "report.pdf", rpc_call=rpc)

    assert exc_info.value.cause is rpc_error
    assert rpc.calls == [
        {
            "method": RPCMethod.ADD_SOURCE_FILE,
            "params": [
                [["report.pdf"]],
                "nb_123",
                [2],
                [1, None, None, None, None, None, None, None, None, None, [1]],
            ],
            "source_path": "/notebook/nb_123",
            "allow_null": False,
            "disable_internal_retries": False,
        }
    ]


@pytest.mark.asyncio
async def test_register_file_source_truncates_large_string_response_preview(
    service: SourceUploadPipeline,
) -> None:
    rpc = RecordingRpc("x" * 5000)

    with pytest.raises(SourceAddError) as exc_info:
        await service.register_file_source("nb_123", "report.pdf", rpc_call=rpc)

    message = str(exc_info.value)
    assert "..." in message
    assert "x" * 300 not in message
    assert len(message) < 320


@pytest.mark.asyncio
async def test_start_resumable_upload_uses_injected_http_client(
    service: SourceUploadPipeline,
) -> None:
    response = MagicMock()
    response.headers = {"x-goog-upload-url": "https://upload.example.com/session"}
    response.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client_cm = AsyncMock()
    client_cm.__aenter__.return_value = client
    client_factory = MagicMock(return_value=client_cm)
    runtime = HttpRuntime()

    upload_url = await service.start_resumable_upload(
        "nb_123",
        "report.pdf",
        12,
        "src_123",
        capabilities=runtime,
        resolve_upload_timeout=lambda default: default,
        async_client_factory=client_factory,
    )

    assert upload_url == "https://upload.example.com/session"
    assert client_factory.call_args.kwargs["cookies"] is runtime.cookies
    request = client.post.await_args
    assert request.kwargs["headers"]["x-goog-upload-command"] == "start"
    assert '"SOURCE_ID": "src_123"' in request.kwargs["content"]
