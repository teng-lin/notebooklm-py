"""Unit tests for the private source upload pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from notebooklm._source_upload import SourceUploadPipeline, _extract_register_file_source_id
from notebooklm.rpc import RPCError, RPCMethod
from notebooklm.types import Source, SourceAddError


class UploadRuntime:
    def __init__(self) -> None:
        self.queue_waits: list[float] = []
        self.labels: list[str] = []
        self.finished: list[str] = []

    def record_upload_queue_wait(self, wait_seconds: float) -> None:
        self.queue_waits.append(wait_seconds)

    def operation_scope(self, log_label: str):
        self.labels.append(log_label)

        @asynccontextmanager
        async def scope() -> AsyncIterator[None]:
            try:
                yield None
            finally:
                self.finished.append(log_label)

        return scope()

    async def rpc_call(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unexpected rpc_call")

    async def transport_post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        raise AssertionError("unexpected transport_post")

    async def next_reqid(self, step: int = 100000) -> int:
        return step

    def assert_bound_loop(self) -> None:
        return None


class HttpRuntime:
    def __init__(self) -> None:
        self._cookies = httpx.Cookies()

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

    @property
    def cookies(self) -> httpx.Cookies:
        return self._cookies


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
    return make_pipeline()


def make_pipeline(
    session: UploadRuntime | None = None,
    kernel: HttpRuntime | None = None,
    auth: HttpRuntime | None = None,
    *,
    max_concurrent_uploads: int | None = None,
    async_client_factory=None,
) -> SourceUploadPipeline:
    session = session or UploadRuntime()
    kernel = kernel or HttpRuntime()
    auth = auth or kernel
    return SourceUploadPipeline(
        session,
        kernel,
        auth,  # type: ignore[arg-type]
        max_concurrent_uploads=max_concurrent_uploads,
        record_upload_queue_wait=session.record_upload_queue_wait,
        async_client_factory=async_client_factory,
    )


def test_extract_register_file_source_id_skips_large_string_candidates() -> None:
    long_payload = " " + ("x" * 2000) + " "

    assert _extract_register_file_source_id([long_payload, "src_123"], "report.pdf") == "src_123"


@pytest.mark.asyncio
async def test_upload_semaphore_is_owned_per_pipeline() -> None:
    first = make_pipeline(max_concurrent_uploads=1)
    second = make_pipeline(max_concurrent_uploads=1)

    assert first.get_upload_semaphore() is first.get_upload_semaphore()
    assert first.get_upload_semaphore() is not second.get_upload_semaphore()


@pytest.mark.asyncio
async def test_add_file_uses_late_bound_hooks_and_finishes_transport(
    tmp_path,
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"hello")
    runtime = UploadRuntime()
    service = make_pipeline(runtime)

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
    assert runtime.labels == ["upload:0"]
    assert runtime.finished == ["upload:0"]
    assert len(runtime.queue_waits) == 1
    register_file_source.assert_awaited_once_with("nb_123", "report.pdf")
    start_resumable_upload.assert_awaited_once_with("nb_123", "report.pdf", 5, "src_123")


@pytest.mark.asyncio
async def test_add_file_operation_scope_wraps_sources_semaphore_wait(tmp_path) -> None:
    first_file = tmp_path / "first.pdf"
    second_file = tmp_path / "second.pdf"
    first_file.write_bytes(b"first")
    second_file.write_bytes(b"second")
    runtime = UploadRuntime()
    service = make_pipeline(runtime, max_concurrent_uploads=1)
    first_streaming_started = asyncio.Event()
    release_first_streaming = asyncio.Event()

    async def upload_file_streaming(_upload_url, file_obj, **kwargs):
        if kwargs["filename"] == "first.pdf":
            first_streaming_started.set()
            await release_first_streaming.wait()
        file_obj.close()

    async def add(path):
        return await service.add_file(
            "nb_123",
            path,
            register_file_source=AsyncMock(return_value=f"src_{path.stem}"),
            start_resumable_upload=AsyncMock(return_value="https://upload.example.com/session"),
            upload_file_streaming=upload_file_streaming,
            wait_until_ready=AsyncMock(),
            wait_until_registered=AsyncMock(),
            rename=AsyncMock(),
            logger=MagicMock(),
        )

    first_task = asyncio.create_task(add(first_file))
    await first_streaming_started.wait()

    second_task = asyncio.create_task(add(second_file))
    while len(runtime.labels) < 2:
        await asyncio.sleep(0)

    assert runtime.labels == ["upload:0", "upload:0"]
    assert len(runtime.queue_waits) == 1

    release_first_streaming.set()
    sources = await asyncio.gather(first_task, second_task)

    assert [source.id for source in sources] == ["src_first", "src_second"]
    assert len(runtime.queue_waits) == 2


@pytest.mark.asyncio
async def test_add_file_custom_title_waits_for_registration_before_rename(
    tmp_path,
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"hello")
    runtime = UploadRuntime()
    service = make_pipeline(runtime)
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
    # A non-transport RPCError must propagate as SourceAddError (the
    # wrapper preserves the original cause). The RPC layer is invoked with
    # ``disable_internal_retries=True`` because register_file_source now
    # owns probe-then-retry recovery via ``idempotent_create``.
    rpc_error = RPCError("bad response")
    rpc = RecordingRpc(rpc_error)

    with pytest.raises(SourceAddError) as exc_info:
        await service.register_file_source(
            "nb_123",
            "report.pdf",
            rpc_call=rpc,
            list_sources=AsyncMock(return_value=[]),
            logger=MagicMock(),
        )

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
            "disable_internal_retries": True,
        }
    ]


@pytest.mark.asyncio
async def test_register_file_source_status3_includes_source_limit_context(
    service: SourceUploadPipeline,
) -> None:
    rpc_error = RPCError(
        "RPC o4cbdc returned null result with status code 3 (Invalid argument).",
        method_id="o4cbdc",
        rpc_code=3,
    )
    rpc = RecordingRpc(rpc_error)
    existing_sources = [
        Source(id=f"source_{index}", title=f"Source {index}") for index in range(56)
    ]
    get_source_limit = AsyncMock(return_value=50)

    with pytest.raises(SourceAddError) as exc_info:
        await service.register_file_source(
            "nb_123",
            "report.pdf",
            rpc_call=rpc,
            list_sources=AsyncMock(return_value=existing_sources),
            get_source_limit=get_source_limit,
            logger=MagicMock(),
        )

    assert exc_info.value.cause is rpc_error
    message = str(exc_info.value)
    assert "56/50 sources" in message
    assert "tier-specific" in message
    assert "per-notebook source limit" in message
    assert "fresh notebook" in message
    get_source_limit.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_register_file_source_truncates_large_string_response_preview(
    service: SourceUploadPipeline,
) -> None:
    rpc = RecordingRpc("x" * 5000)

    with pytest.raises(SourceAddError) as exc_info:
        await service.register_file_source(
            "nb_123",
            "report.pdf",
            rpc_call=rpc,
            list_sources=AsyncMock(return_value=[]),
            logger=MagicMock(),
        )

    message = str(exc_info.value)
    assert "..." in message
    assert "x" * 300 not in message
    assert len(message) < 320


@pytest.mark.asyncio
async def test_start_resumable_upload_uses_injected_http_client() -> None:
    response = MagicMock()
    response.headers = {"x-goog-upload-url": "https://upload.example.com/session"}
    response.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client_cm = AsyncMock()
    client_cm.__aenter__.return_value = client
    client_factory = MagicMock(return_value=client_cm)
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)

    upload_url = await service.start_resumable_upload(
        "nb_123",
        "report.pdf",
        12,
        "src_123",
    )

    assert upload_url == "https://upload.example.com/session"
    assert client_factory.call_args.kwargs["cookies"] is runtime.cookies
    request = client.post.await_args
    assert request.kwargs["headers"]["x-goog-upload-command"] == "start"
    assert '"SOURCE_ID": "src_123"' in request.kwargs["content"]
