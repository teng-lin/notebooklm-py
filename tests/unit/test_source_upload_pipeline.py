"""Unit tests for the private source upload pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from notebooklm._source_upload import (
    SourceUploadPipeline,
    _extract_register_file_source_id,
    _redact_upload_url,
    _transient_error_types_for_upload,
    _validate_resumable_upload_url,
)
from notebooklm.exceptions import NetworkError, ValidationError
from notebooklm.rpc import RPCError, RPCMethod
from notebooklm.types import Source, SourceAddError


class UploadRuntime:
    """Test stub bundling ``rpc_call`` + ``operation_scope`` +
    ``assert_bound_loop`` on a single object so one instance can be
    passed as all three of :class:`SourceUploadPipeline`'s ``rpc`` /
    ``drain`` / ``lifecycle`` collaborator slots. (The production
    composite Protocol of the same name was retired together with its
    adapter dataclass; this stub kept the historical name to minimise
    churn across the test file.)
    """

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
    # The ``UploadRuntime`` test stub bundles ``rpc_call``,
    # ``operation_scope``, and ``assert_bound_loop`` so a single instance
    # structurally satisfies all three of the constructor's
    # ``rpc`` / ``drain`` / ``lifecycle`` collaborator slots.
    return SourceUploadPipeline(
        rpc=session,  # type: ignore[arg-type]
        drain=session,  # type: ignore[arg-type]
        lifecycle=session,  # type: ignore[arg-type]
        kernel=kernel,
        auth=auth,  # type: ignore[arg-type]
        max_concurrent_uploads=max_concurrent_uploads,
        record_upload_queue_wait=session.record_upload_queue_wait,
        async_client_factory=async_client_factory,
    )


def test_extract_register_file_source_id_accepts_known_response_shapes() -> None:
    assert _extract_register_file_source_id([["src_123"]], "report.pdf") == "src_123"
    assert _extract_register_file_source_id([[[["src_123"]]]], "report.pdf") == "src_123"
    assert _extract_register_file_source_id({"SOURCE_ID": "src_123"}, "report.pdf") == "src_123"
    assert (
        _extract_register_file_source_id(
            {"source": {"SOURCE_ID": "src_123", "SOURCE_NAME": "report.pdf"}},
            "report.pdf",
        )
        == "src_123"
    )
    assert (
        _extract_register_file_source_id([[[["src_123"], "report.pdf", [None]]]], "report.pdf")
        == "src_123"
    )
    assert (
        _extract_register_file_source_id([[[["report.pdf", ["src_456"], [None]]]]], "report.pdf")
        == "src_456"
    )
    assert _extract_register_file_source_id([None, [[["src_789"]]]], "report.pdf") == "src_789"
    assert (
        _extract_register_file_source_id({"id": "src_123", "title": "report.pdf"}, "report.pdf")
        == "src_123"
    )


def test_extract_register_file_source_id_skips_large_string_candidates() -> None:
    long_payload = " " + ("x" * 2000) + " "

    assert (
        _extract_register_file_source_id(
            {
                "debug": {"SOURCE_ID": long_payload},
                "source": {"SOURCE_ID": "src_123", "SOURCE_NAME": "report.pdf"},
            },
            "report.pdf",
        )
        == "src_123"
    )


def test_extract_register_file_source_id_rejects_ambiguous_nested_ids() -> None:
    unrelated_uuid = "11111111-2222-3333-4444-555555555555"

    assert (
        _extract_register_file_source_id(
            {"debug": [["trace", unrelated_uuid]], "status": "ok"},
            "report.pdf",
        )
        is None
    )
    assert (
        _extract_register_file_source_id(
            {"debug": {"SOURCE_ID": unrelated_uuid}},
            "report.pdf",
        )
        is None
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://notebooklm.google.com/upload/_/?upload_id=session",
        "https://notebooklm.google.com:443/upload/_/?upload_id=session",
        "https://notebooklm.google.com/upload/_//?upload_id=session",
    ],
)
def test_validate_resumable_upload_url_accepts_configured_upload_endpoint(url: str) -> None:
    assert _validate_resumable_upload_url(url) == url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://user:pass@notebooklm.google.com/upload/_/?upload_id=SECRET_UPLOAD_ID",
            "https://notebooklm.google.com/upload/_/?...",
        ),
        (
            "https://user:pass@[2001:db8::1]:443/upload/_/?upload_id=SECRET_UPLOAD_ID",
            "https://[2001:db8::1]:443/upload/_/?...",
        ),
        ("https://notebooklm.google.com:bad/upload/_/?upload_id=SECRET", "[REDACTED_UPLOAD_URL]"),
    ],
)
def test_redact_upload_url_omits_userinfo_and_query_secrets(url: str, expected: str) -> None:
    redacted = _redact_upload_url(url)

    assert redacted == expected
    assert "user" not in redacted
    assert "pass" not in redacted
    assert "SECRET" not in redacted


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("http://notebooklm.google.com/upload/_/?upload_id=session", "must use https"),
        ("https://evil.example/upload/_/?upload_id=session", "host is not trusted"),
        ("https://notebooklm.google.com/other/_/?upload_id=session", "path is not trusted"),
        ("https://notebooklm.google.com/upload/_/", "exactly one non-empty upload_id"),
        ("https://notebooklm.google.com/upload/_/?upload_id=", "exactly one non-empty upload_id"),
        (
            "https://notebooklm.google.com/upload/_/?upload_id=one&upload_id=two",
            "exactly one non-empty upload_id",
        ),
    ],
)
def test_validate_resumable_upload_url_rejects_untrusted_shapes(url: str, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _validate_resumable_upload_url(url)


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("audio/mpeg", (10, 0, None)),
        ("Audio/MPEG", (10, 0, None)),
        ("audio/mpeg; codecs=mp3", (10, 0, None)),
        ("application/mp4", (10, 0, None)),
        ("application/ogg", (10, 0, None)),
        ("application/x-matroska", (10, 0, None)),
        ("application/pdf", ()),
    ],
)
def test_transient_error_types_for_upload_classifies_media_content_types(
    content_type: str, expected: tuple[int | None, ...]
) -> None:
    assert _transient_error_types_for_upload(content_type) == expected


@pytest.mark.asyncio
async def test_upload_semaphore_is_owned_per_pipeline() -> None:
    first = make_pipeline(max_concurrent_uploads=1)
    second = make_pipeline(max_concurrent_uploads=1)

    assert first.get_upload_semaphore() is first.get_upload_semaphore()
    assert first.get_upload_semaphore() is not second.get_upload_semaphore()


@pytest.mark.asyncio
async def test_set_bound_loop_discards_semaphore_on_loop_change() -> None:
    """Issue #1196 upload variant: a loop change must drop the cached semaphore.

    The upload semaphore is bound to whichever loop it was first built under;
    on a close→reopen onto a different loop the stale semaphore must not be
    reused (it would raise "bound to a different event loop" on 3.10/3.11).
    ``set_bound_loop`` discards it whenever the bound loop actually changes,
    while a repeat of the *same* loop leaves it intact.
    """
    pipeline = make_pipeline(max_concurrent_uploads=1)
    loop_a = asyncio.get_running_loop()

    pipeline.set_bound_loop(loop_a)
    sem_a = pipeline.get_upload_semaphore()
    # Re-binding to the same loop must NOT discard the cached semaphore.
    pipeline.set_bound_loop(loop_a)
    assert pipeline.get_upload_semaphore() is sem_a

    # A loop change discards the stale semaphore so the next access rebuilds it.
    other_loop = asyncio.new_event_loop()
    try:
        pipeline.set_bound_loop(other_loop)
        assert pipeline._upload_semaphore is None
        assert pipeline.get_upload_semaphore() is not sem_a
    finally:
        other_loop.close()


@pytest.mark.asyncio
async def test_reset_after_open_drops_semaphore() -> None:
    """``reset_after_open`` clears the lazy semaphore so a reopen rebuilds it."""
    pipeline = make_pipeline(max_concurrent_uploads=1)
    sem = pipeline.get_upload_semaphore()
    assert pipeline._upload_semaphore is sem

    pipeline.reset_after_open()
    assert pipeline._upload_semaphore is None
    # ``max_concurrent_uploads`` is untouched, so the rebuilt semaphore keeps
    # the same cap and is a fresh object.
    rebuilt = pipeline.get_upload_semaphore()
    assert rebuilt is not sem


@pytest.mark.asyncio
async def test_add_file_uses_pipeline_steps_and_finishes_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"hello")
    runtime = UploadRuntime()
    service = make_pipeline(runtime)

    register_file_source = AsyncMock(return_value="src_123")
    start_resumable_upload = AsyncMock(
        return_value="https://notebooklm.google.com/upload/_/?upload_id=session"
    )

    async def upload_file_streaming(upload_url, file_obj, **kwargs):
        assert upload_url == "https://notebooklm.google.com/upload/_/?upload_id=session"
        assert file_obj.read() == b"hello"
        assert kwargs["filename"] == "report.pdf"
        assert kwargs["total_bytes"] == 5
        file_obj.close()

    monkeypatch.setattr(service, "register_file_source", register_file_source)
    monkeypatch.setattr(service, "start_resumable_upload", start_resumable_upload)
    monkeypatch.setattr(service, "upload_file_streaming", upload_file_streaming)
    monkeypatch.setattr(service, "wait_until_ready", AsyncMock())
    monkeypatch.setattr(service, "wait_until_registered", AsyncMock())
    monkeypatch.setattr(service, "rename", AsyncMock())

    source = await service.add_file(
        "nb_123",
        file_path,
    )

    assert source.id == "src_123"
    assert source.title == "report.pdf"
    assert source.is_processing
    assert runtime.labels == ["upload:0"]
    assert runtime.finished == ["upload:0"]
    assert len(runtime.queue_waits) == 1
    register_file_source.assert_awaited_once_with("nb_123", "report.pdf")
    start_resumable_upload.assert_awaited_once_with(
        "nb_123", "report.pdf", 5, "src_123", "application/pdf"
    )


@pytest.mark.parametrize(
    ("filename", "mime_type"),
    [
        ("article.html", None),
        ("ARTICLE.HTML", None),
        ("article.htm", None),
        ("article.xhtml", None),
        ("article.xht", None),
        ("article.txt", "text/html"),
        ("article.txt", "Text/HTML; charset=utf-8"),
        ("article.xhtml", "application/xhtml+xml"),
    ],
)
@pytest.mark.asyncio
async def test_add_file_rejects_html_before_registering_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    filename: str,
    mime_type: str | None,
) -> None:
    file_path = tmp_path / filename
    file_path.write_text("<html><body><h1>Article</h1></body></html>", encoding="utf-8")
    service = make_pipeline()
    register_file_source = AsyncMock(return_value="src_123")
    start_resumable_upload = AsyncMock()

    monkeypatch.setattr(service, "register_file_source", register_file_source)
    monkeypatch.setattr(service, "start_resumable_upload", start_resumable_upload)
    monkeypatch.setattr(service, "upload_file_streaming", AsyncMock())

    with pytest.raises(ValidationError, match="HTML file uploads are not supported"):
        await service.add_file("nb_123", file_path, mime_type=mime_type)

    register_file_source.assert_not_awaited()
    start_resumable_upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_file_operation_scope_wraps_sources_semaphore_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
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

    register_file_source = AsyncMock(
        side_effect=lambda _notebook_id, filename: f"src_{filename.removesuffix('.pdf')}"
    )
    monkeypatch.setattr(service, "register_file_source", register_file_source)
    monkeypatch.setattr(
        service,
        "start_resumable_upload",
        AsyncMock(return_value="https://notebooklm.google.com/upload/_/?upload_id=session"),
    )
    monkeypatch.setattr(service, "upload_file_streaming", upload_file_streaming)
    monkeypatch.setattr(service, "wait_until_ready", AsyncMock())
    monkeypatch.setattr(service, "wait_until_registered", AsyncMock())
    monkeypatch.setattr(service, "rename", AsyncMock())

    async def add(path):
        return await service.add_file(
            "nb_123",
            path,
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
    monkeypatch: pytest.MonkeyPatch,
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

    monkeypatch.setattr(service, "register_file_source", AsyncMock(return_value="src_123"))
    monkeypatch.setattr(
        service,
        "start_resumable_upload",
        AsyncMock(return_value="https://notebooklm.google.com/upload/_/?upload_id=session"),
    )
    monkeypatch.setattr(service, "upload_file_streaming", upload_file_streaming)
    monkeypatch.setattr(service, "wait_until_ready", AsyncMock())
    monkeypatch.setattr(service, "wait_until_registered", wait_until_registered)
    monkeypatch.setattr(service, "rename", rename)

    source = await service.add_file(
        "nb_123",
        file_path,
        title="  Custom  ",
        wait_timeout=45.0,
    )

    assert source == Source(id="src_123", title="Custom", _type_code=7, url="https://source")
    wait_until_registered.assert_awaited_once_with(
        "nb_123", "src_123", timeout=45.0, transient_error_types=()
    )
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
async def test_register_file_source_uses_configured_source_limit_lookup(
    service: SourceUploadPipeline,
    monkeypatch: pytest.MonkeyPatch,
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
    list_sources = AsyncMock(return_value=existing_sources)
    get_source_limit = AsyncMock(return_value=50)
    monkeypatch.setattr(service, "list_sources", list_sources)
    service.configure_source_limit_lookup(get_source_limit)

    with pytest.raises(SourceAddError) as exc_info:
        await service.register_file_source(
            "nb_123",
            "report.pdf",
            rpc_call=rpc,
        )

    assert "56/50 sources" in str(exc_info.value)
    list_sources.assert_awaited_once_with("nb_123")
    get_source_limit.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_register_file_source_ambiguous_response_falls_back_to_probe(
    service: SourceUploadPipeline,
) -> None:
    unrelated_uuid = "11111111-2222-3333-4444-555555555555"
    rpc = RecordingRpc({"debug": [["trace", unrelated_uuid]], "status": "ok"})
    list_sources = AsyncMock(
        side_effect=[
            [],
            [Source(id="src_probe", title="report.pdf")],
        ]
    )

    source_id = await service.register_file_source(
        "nb_123",
        "report.pdf",
        rpc_call=rpc,
        list_sources=list_sources,
        logger=MagicMock(),
    )

    assert source_id == "src_probe"
    assert [call.args for call in list_sources.await_args_list] == [
        ("nb_123",),
        ("nb_123",),
    ]


@pytest.mark.asyncio
async def test_register_file_source_pre_existing_response_id_falls_back_to_probe(
    service: SourceUploadPipeline,
) -> None:
    rpc = RecordingRpc([["src_existing"]])
    list_sources = AsyncMock(
        side_effect=[
            [Source(id="src_existing", title="old.pdf")],
            [
                Source(id="src_existing", title="old.pdf"),
                Source(id="src_new", title="report.pdf"),
            ],
        ]
    )

    source_id = await service.register_file_source(
        "nb_123",
        "report.pdf",
        rpc_call=rpc,
        list_sources=list_sources,
        logger=MagicMock(),
    )

    assert source_id == "src_new"


@pytest.mark.asyncio
async def test_register_file_source_probe_failure_is_typed_and_sanitized(
    service: SourceUploadPipeline,
) -> None:
    unrelated_uuid = "11111111-2222-3333-4444-555555555555"
    secret = "SECRET_UPLOAD_ID"
    rpc = RecordingRpc({"debug": [["trace", unrelated_uuid]], "upload": secret})
    list_sources = AsyncMock(side_effect=[[], NetworkError(f"network leaked {secret}")])

    with pytest.raises(SourceAddError) as exc_info:
        await service.register_file_source(
            "nb_123",
            "report.pdf",
            rpc_call=rpc,
            list_sources=list_sources,
            logger=MagicMock(),
        )

    message = str(exc_info.value)
    assert exc_info.value.cause is not None
    assert "source-list probe failed (NetworkError)" in message
    assert unrelated_uuid not in message
    assert secret not in message


@pytest.mark.asyncio
async def test_register_file_source_sanitizes_untrusted_response_error(
    service: SourceUploadPipeline,
) -> None:
    secret = "SECRET_UPLOAD_ID"
    rpc = RecordingRpc(f"{secret}{'x' * 5000}")

    with pytest.raises(SourceAddError) as exc_info:
        await service.register_file_source(
            "nb_123",
            "report.pdf",
            rpc_call=rpc,
            list_sources=AsyncMock(return_value=[]),
            logger=MagicMock(),
        )

    message = str(exc_info.value)
    assert "string registration response" in message
    assert secret not in message
    assert "x" * 300 not in message
    assert len(message) < 320


@pytest.mark.asyncio
async def test_start_resumable_upload_uses_injected_http_client() -> None:
    response = MagicMock()
    response.headers = {
        "x-goog-upload-url": "https://notebooklm.google.com/upload/_/?upload_id=session"
    }
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
        "application/pdf",
    )

    assert upload_url == "https://notebooklm.google.com/upload/_/?upload_id=session"
    assert client_factory.call_args.kwargs["cookies"] is runtime.cookies
    request = client.post.await_args
    assert request.kwargs["headers"]["x-goog-upload-command"] == "start"
    assert request.kwargs["headers"]["x-goog-upload-header-content-type"] == "application/pdf"
    assert '"SOURCE_ID": "src_123"' in request.kwargs["content"]


@pytest.mark.asyncio
async def test_start_resumable_upload_rejects_untrusted_upload_header_url() -> None:
    response = MagicMock()
    response.headers = {"x-goog-upload-url": "https://evil.example/upload/_/?upload_id=secret"}
    response.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client_cm = AsyncMock()
    client_cm.__aenter__.return_value = client
    client_factory = MagicMock(return_value=client_cm)
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)

    with pytest.raises(SourceAddError, match="invalid resumable upload URL"):
        await service.start_resumable_upload(
            "nb_123",
            "report.pdf",
            12,
            "src_123",
            "application/pdf",
        )


@pytest.mark.asyncio
async def test_upload_file_streaming_rejects_untrusted_url_before_post(tmp_path) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"content")
    client_factory = MagicMock()
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)

    with pytest.raises(ValidationError, match="host is not trusted"):
        await service.upload_file_streaming(
            "https://evil.example/upload/_/?upload_id=secret",
            file_path,
        )

    client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_upload_file_streaming_redacts_upload_url_in_debug_logs(tmp_path) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"content")
    response = MagicMock()
    response.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client_cm = AsyncMock()
    client_cm.__aenter__.return_value = client
    client_factory = MagicMock(return_value=client_cm)
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)
    logger = MagicMock()

    await service.upload_file_streaming(
        "https://notebooklm.google.com/upload/_/?upload_id=SECRET_UPLOAD_ID",
        file_path,
        logger=logger,
    )

    debug_messages = [str(call) for call in logger.debug.call_args_list]
    assert all("SECRET_UPLOAD_ID" not in message for message in debug_messages)
    assert any(
        "https://notebooklm.google.com/upload/_/?..." in message for message in debug_messages
    )


@pytest.mark.asyncio
async def test_cancel_upload_session_redacts_credentials_on_validation_failure() -> None:
    client_factory = MagicMock()
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)
    logger = MagicMock()

    await service.cancel_upload_session(
        "https://alice:s3cr3t@notebooklm.google.com/upload/_/?upload_id=SECRET_UPLOAD_ID",
        "https://notebooklm.google.com",
        "0",
        logger=logger,
    )

    client_factory.assert_not_called()
    debug_messages = [str(call) for call in logger.debug.call_args_list]
    assert any(
        "https://notebooklm.google.com/upload/_/?..." in message for message in debug_messages
    )
    assert all("alice" not in message for message in debug_messages)
    assert all("s3cr3t" not in message for message in debug_messages)
    assert all("SECRET_UPLOAD_ID" not in message for message in debug_messages)
