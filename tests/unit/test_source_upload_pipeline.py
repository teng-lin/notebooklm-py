"""Unit tests for the private source upload pipeline."""

from __future__ import annotations

import asyncio
import builtins
import io
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._sources import SourcesAPI
from notebooklm._web.sources.upload import (
    SourceUploadPipeline,
    _extract_register_file_source_id,
    _redact_upload_url,
    _transient_error_types_for_upload,
    _upload_url_origin,
    _validate_resumable_upload_url,
)
from notebooklm.exceptions import (
    AuthError,
    NetworkError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from notebooklm.rpc import RPCError, RPCMethod
from notebooklm.types import Source, SourceAddError


class UploadRuntime:
    """RPC and supervisor stub for an active upload generation."""

    def __init__(self) -> None:
        self.queue_waits: list[float] = []
        self.labels: list[str] = []
        self.finished: list[str] = []

    def record_upload_queue_wait(self, wait_seconds: float) -> None:
        self.queue_waits.append(wait_seconds)

    def operation_scope(self, log_label: str):
        self.labels.append(log_label)

        @asynccontextmanager
        async def scope() -> AsyncIterator[SimpleNamespace]:
            try:
                yield SimpleNamespace(epoch=1)
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

    async def spawn_child(self, label: str, factory: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(factory(), name=label)


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

    def get_http_client(self, *, expected_epoch: int) -> HttpRuntime:
        assert expected_epoch == 1
        return self


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
    supervisor: UploadRuntime | None = None,
    max_concurrent_uploads: int | None = None,
    async_client_factory=None,
) -> SourceUploadPipeline:
    session = session or UploadRuntime()
    kernel = kernel or HttpRuntime()
    auth = auth or kernel
    pipeline = SourceUploadPipeline(
        rpc=session,  # type: ignore[arg-type]
        supervisor=supervisor or session,  # type: ignore[arg-type]
        kernel=kernel,
        auth=auth,  # type: ignore[arg-type]
        max_concurrent_uploads=max_concurrent_uploads,
        record_upload_queue_wait=session.record_upload_queue_wait,
        async_client_factory=async_client_factory,
    )
    # Direct pipeline tests model the web/upload transports after lifecycle
    # open has activated generation 1 and installed the resource registry.
    pipeline._active_epoch = 1
    pipeline._closing = False
    pipeline._registry_lock = asyncio.Lock()
    return pipeline


def track_opened_files(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    opened_files: list[Any] = []
    real_open = builtins.open

    def tracked_open(*args, **kwargs):
        file_obj = real_open(*args, **kwargs)
        opened_files.append(file_obj)
        return file_obj

    monkeypatch.setattr(builtins, "open", tracked_open)
    return opened_files


def upload_client_factory(failure: BaseException) -> MagicMock:
    client = AsyncMock()
    client.post = AsyncMock(side_effect=failure)
    client.__aenter__.return_value = client
    return MagicMock(return_value=client)


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


# --- upload-host trust is host-RELATIVE, never a constant ------------------
# Google serves the personal app from two interchangeable hosts after the
# "Gemini Notebook" rebrand, and its Scotty frontend picks which one it names in
# the ``X-Goog-Upload-URL`` response header — so a personal client must accept
# either. An enterprise tenant stays pinned to exactly its own host: widening to
# a constant personal-host set would let a response header redirect ENTERPRISE
# file bytes to the consumer service.
_PERSONAL_HOSTS = ("notebooklm.google.com", "notebook.google.com")
_ENTERPRISE_HOST = "notebooklm.cloud.google.com"


@pytest.mark.parametrize("named_host", _PERSONAL_HOSTS)
@pytest.mark.parametrize("configured_host", _PERSONAL_HOSTS)
def test_validate_resumable_upload_url_accepts_either_personal_host(
    monkeypatch: pytest.MonkeyPatch, configured_host: str, named_host: str
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", f"https://{configured_host}")
    url = f"https://{named_host}/upload/_/?upload_id=session"

    assert _validate_resumable_upload_url(url) == url


@pytest.mark.parametrize("named_host", _PERSONAL_HOSTS)
def test_validate_resumable_upload_url_pins_enterprise_to_its_own_host(
    monkeypatch: pytest.MonkeyPatch, named_host: str
) -> None:
    """An enterprise tenant must reject a consumer-host upload URL (data boundary)."""
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", f"https://{_ENTERPRISE_HOST}")

    with pytest.raises(ValidationError, match="host is not trusted"):
        _validate_resumable_upload_url(f"https://{named_host}/upload/_/?upload_id=session")

    own_host_url = f"https://{_ENTERPRISE_HOST}/upload/_/?upload_id=session"
    assert _validate_resumable_upload_url(own_host_url) == own_host_url


@pytest.mark.parametrize(
    "named_host",
    [
        "evil.example",
        "notebooklm.google",  # the marketing host — no ``.com``
        "notebooklm.google.com.evil.example",  # suffix-confusion
        "upload.notebooklm.google.com",  # subdomain, not an app host
        "storage.googleapis.com",
    ],
)
@pytest.mark.parametrize("configured_host", (*_PERSONAL_HOSTS, _ENTERPRISE_HOST))
def test_validate_resumable_upload_url_still_rejects_foreign_hosts(
    monkeypatch: pytest.MonkeyPatch, configured_host: str, named_host: str
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", f"https://{configured_host}")

    with pytest.raises(ValidationError, match="host is not trusted"):
        _validate_resumable_upload_url(f"https://{named_host}/upload/_/?upload_id=session")


def test_validate_resumable_upload_url_keeps_non_host_guards_on_the_alias_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widening the *host* set must not relax the port/scheme/path/userinfo guards."""
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", f"https://{_PERSONAL_HOSTS[0]}")
    alias = _PERSONAL_HOSTS[1]

    for url, match in (
        (f"https://{alias}:8443/upload/_/?upload_id=session", "host is not trusted"),
        (f"http://{alias}/upload/_/?upload_id=session", "must use https"),
        (f"https://{alias}/other/_/?upload_id=session", "path is not trusted"),
        (f"https://u:p@{alias}/upload/_/?upload_id=session", "must not contain credentials"),
        (f"https://{alias}/upload/_/", "exactly one non-empty upload_id"),
    ):
        with pytest.raises(ValidationError, match=match):
            _validate_resumable_upload_url(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://notebook.google.com/upload/_/?upload_id=s", "https://notebook.google.com"),
        (
            "https://notebooklm.google.com:443/upload/_/?upload_id=s",
            "https://notebooklm.google.com",
        ),
    ],
)
def test_upload_url_origin_drops_path_query_and_default_port(url: str, expected: str) -> None:
    assert _upload_url_origin(url) == expected


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
        finalize_uploaded=SourcesAPI._finalize_uploaded_file,
    )

    assert source.id == "src_123"
    assert source.title == "report.pdf"
    assert source.is_processing
    assert runtime.labels == ["upload:0"]
    assert runtime.finished == ["upload:0"]
    assert len(runtime.queue_waits) == 1
    register_file_source.assert_awaited_once_with("nb_123", "report.pdf")
    start_resumable_upload.assert_awaited_once_with(
        "nb_123",
        "report.pdf",
        5,
        "src_123",
        "application/pdf",
        expected_epoch=1,
    )


@pytest.mark.parametrize(
    ("failing_step", "expected_stage"),
    [
        ("start", "start_session"),
        ("upload", "upload_finalize"),
    ],
)
@pytest.mark.asyncio
async def test_add_file_surfaces_registered_source_when_upload_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    failing_step: str,
    expected_stage: str,
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"hello")
    failure = ValidationError(f"{failing_step} failed")
    factory = upload_client_factory(failure) if failing_step == "upload" else None
    service = make_pipeline(async_client_factory=factory)
    opened_files = track_opened_files(monkeypatch)

    monkeypatch.setattr(service, "register_file_source", AsyncMock(return_value="src_123"))
    monkeypatch.setattr(
        service,
        "start_resumable_upload",
        AsyncMock(
            side_effect=failure if failing_step == "start" else None,
            return_value="https://notebooklm.google.com/upload/_/?upload_id=session",
        ),
    )

    with pytest.raises(ValidationError) as exc_info:
        await service.add_file(
            "nb_123", file_path, finalize_uploaded=SourcesAPI._finalize_uploaded_file
        )

    error = exc_info.value
    # Unwrapped: the real cause propagates directly, with recovery context
    # attached rather than wrapped in a new exception type.
    assert error is failure
    assert error.source_id == "src_123"  # type: ignore[attr-defined]
    assert error.stage == expected_stage  # type: ignore[attr-defined]
    assert opened_files and opened_files[0].closed


@pytest.mark.parametrize(
    ("failing_step", "expected_stage"),
    [("start", "start_session"), ("upload", "upload_finalize")],
)
@pytest.mark.asyncio
async def test_transport_failure_is_typed_as_network_error_not_input_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    failing_step: str,
    expected_stage: str,
) -> None:
    """A dropped connection must not be projected as a per-source input rejection.

    ``upload_file_streaming`` POSTs through a bare ``httpx.AsyncClient``, so a
    mid-body reset surfaces as an untyped ``httpx.RequestError``. Left raw, the
    cause-based classification in ``_app/errors.py`` has nothing to match on and
    falls through to ``SOURCE_ADD`` — projected to REST/MCP as HTTP 422 "your
    input is unacceptable", which tells a caller not to retry a failure that is
    entirely retryable.
    """
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"hello")
    reset = httpx.RequestError("connection reset by peer")
    factory = upload_client_factory(reset) if failing_step == "upload" else None
    service = make_pipeline(async_client_factory=factory)
    track_opened_files(monkeypatch)

    monkeypatch.setattr(service, "register_file_source", AsyncMock(return_value="src_123"))
    monkeypatch.setattr(
        service,
        "start_resumable_upload",
        AsyncMock(
            side_effect=reset if failing_step == "start" else None,
            return_value="https://notebooklm.google.com/upload/_/?upload_id=session",
        ),
    )

    with pytest.raises(NetworkError) as exc_info:
        await service.add_file(
            "nb_123", file_path, finalize_uploaded=SourcesAPI._finalize_uploaded_file
        )

    error = exc_info.value
    assert error.source_id == "src_123"  # type: ignore[attr-defined]
    assert error.stage == expected_stage  # type: ignore[attr-defined]
    # Typed, so the classifier routes it as NETWORK, not SOURCE_ADD…
    assert error.original_error is reset
    # …without losing the httpx exception it came from.
    assert error.__cause__ is reset


@pytest.mark.asyncio
async def test_add_file_does_not_wrap_registration_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"hello")
    service = make_pipeline()
    opened_files = track_opened_files(monkeypatch)
    failure = SourceAddError("report.pdf", message="registration failed")
    monkeypatch.setattr(service, "register_file_source", AsyncMock(side_effect=failure))
    start = AsyncMock()
    monkeypatch.setattr(service, "start_resumable_upload", start)

    with pytest.raises(SourceAddError) as exc_info:
        await service.add_file(
            "nb_123", file_path, finalize_uploaded=SourcesAPI._finalize_uploaded_file
        )

    assert exc_info.value is failure
    assert not hasattr(exc_info.value, "source_id")
    assert opened_files and opened_files[0].closed
    start.assert_not_awaited()


@pytest.mark.parametrize("cancel_stage", ["start", "upload"])
@pytest.mark.asyncio
async def test_add_file_does_not_wrap_cancellation_after_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path, cancel_stage: str
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"hello")
    cancelled = asyncio.CancelledError()
    factory = upload_client_factory(cancelled) if cancel_stage == "upload" else None
    service = make_pipeline(async_client_factory=factory)
    opened_files = track_opened_files(monkeypatch)
    monkeypatch.setattr(service, "register_file_source", AsyncMock(return_value="src_123"))
    monkeypatch.setattr(
        service,
        "start_resumable_upload",
        AsyncMock(
            side_effect=cancelled if cancel_stage == "start" else None,
            return_value="https://notebooklm.google.com/upload/_/?upload_id=session",
        ),
    )

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await service.add_file(
            "nb_123", file_path, finalize_uploaded=SourcesAPI._finalize_uploaded_file
        )

    # The contract is that cancellation stays cancellation — ``except Exception``
    # in the partial-upload wrap must not see it (``CancelledError`` is a
    # ``BaseException``). Asserted by type, not identity: on the ``upload`` leg the
    # cancellation crosses ``asyncio.shield(finalize_task)``, and on Python 3.10
    # ``Task.__wakeup`` re-raises a FRESH ``CancelledError`` rather than
    # propagating the instance the test constructed (3.11+ preserves it). Identity
    # would pin an interpreter detail; not-wrapped is the behaviour we care about.
    assert not hasattr(exc_info.value, "source_id")
    if cancel_stage == "start":
        assert exc_info.value is cancelled
    assert opened_files and opened_files[0].closed


@pytest.mark.asyncio
async def test_upload_failure_closes_file_object_after_ownership_transfer() -> None:
    """Passing an IO object transfers ownership to the upload pipeline."""
    request = httpx.Request("POST", "https://notebooklm.google.com/upload/_/")
    response = httpx.Response(400, request=request)
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client_factory = MagicMock(return_value=client)
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)
    file_obj = io.BytesIO(b"hello")

    with pytest.raises(ValidationError):
        await service.upload_file_streaming(
            "https://notebooklm.google.com/upload/_/?upload_id=session",
            file_obj,
            filename="report.pdf",
            total_bytes=5,
            expected_epoch=1,
        )

    assert file_obj.closed


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
        await service.add_file(
            "nb_123",
            file_path,
            mime_type=mime_type,
            finalize_uploaded=SourcesAPI._finalize_uploaded_file,
        )

    register_file_source.assert_not_awaited()
    start_resumable_upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_workflow_validates_mime_before_admission_but_resolves_path_inside(
    tmp_path,
) -> None:
    runtime = UploadRuntime()
    service = make_pipeline(runtime, supervisor=runtime)

    with pytest.raises(ValidationError, match="HTML file uploads are not supported"):
        await service.add_file(
            "nb_123",
            tmp_path / "missing.html",
            finalize_uploaded=SourcesAPI._finalize_uploaded_file,
        )
    assert runtime.labels == []

    with pytest.raises(FileNotFoundError, match="File not found"):
        await service.add_file(
            "nb_123",
            tmp_path / "missing.pdf",
            finalize_uploaded=SourcesAPI._finalize_uploaded_file,
        )
    assert runtime.labels == ["upload:0"]
    assert runtime.finished == ["upload:0"]


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
            finalize_uploaded=SourcesAPI._finalize_uploaded_file,
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
        title="Custom",
        wait_timeout=45.0,
        finalize_uploaded=SourcesAPI._finalize_uploaded_file,
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
            # Nested template block per the Gemini-3.5 wire migration (#1546).
            "params": [
                [["report.pdf"]],
                "nb_123",
                [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
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
        "The server rejected this request (invalid argument).",
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
        "The server rejected this request (invalid argument).",
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
async def test_register_file_source_probe_decode_failure_aborts_instead_of_retrying(
    service: SourceUploadPipeline,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A probe that cannot answer aborts the registration, not retries it (#2220).

    Sharper here than on the URL paths: the probe's answer is the source id the
    file bytes are subsequently streamed into, so guessing "no match" does not
    merely risk a duplicate row — it can direct an upload at the wrong source.
    The registration must fire once and the failure must surface.
    """
    logger = logging.getLogger("tests.upload_pipeline_probe")
    rpc = RecordingRpc(NetworkError("commit lost"))
    probe_error = RPCError("probe decode failed")
    # Baseline succeeds; the probe that follows the transport failure does not.
    # Exactly two: baseline, then the failing probe (see the add_url twin).
    list_sources = AsyncMock(side_effect=[[], probe_error])

    with (
        caplog.at_level(logging.WARNING, logger=logger.name),
        pytest.raises(SourceAddError) as exc_info,
    ):
        await service.register_file_source(
            "nb_123",
            "report.pdf",
            rpc_call=rpc,
            list_sources=list_sources,
            logger=logger,
        )

    # The load-bearing assertion: ONE register attempt. Restore the probe's
    # ``return None`` and this becomes 2.
    assert len(rpc.calls) == 1
    assert exc_info.value.cause is probe_error
    assert "Cannot confirm file source" in str(exc_info.value)
    assert "will not be retried" in caplog.text


@pytest.mark.asyncio
async def test_register_file_source_baseline_failure_warns_but_proceeds(
    service: SourceUploadPipeline,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed *baseline* still degrades — at WARNING, not DEBUG (#2220).

    The parity half of the issue. Unlike the probe, the baseline runs before
    anything is written, so proceeding is correct; what was wrong is that the
    ``notebooklm`` logger defaults to WARNING, so the DEBUG record was dropped
    before any handler saw it and the call ran with a degraded probe in silence.
    """
    logger = logging.getLogger("tests.upload_pipeline_baseline")
    rpc = RecordingRpc([[["src_new"]]])
    list_sources = AsyncMock(side_effect=RPCError("baseline decode failed"))

    with caplog.at_level(logging.WARNING, logger=logger.name):
        source_id = await service.register_file_source(
            "nb_123",
            "report.pdf",
            rpc_call=rpc,
            list_sources=list_sources,
            logger=logger,
        )

    assert source_id == "src_new"
    # Emitted at a level the default configuration actually passes through.
    assert "baseline list() failed (RPCError)" in caplog.text


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
    client.__aenter__.return_value = client
    client_factory = MagicMock(return_value=client)
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)

    upload_url = await service.start_resumable_upload(
        "nb_123",
        "report.pdf",
        12,
        "src_123",
        "application/pdf",
        expected_epoch=1,
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
    client.__aenter__.return_value = client
    client_factory = MagicMock(return_value=client)
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)

    with pytest.raises(SourceAddError, match="invalid resumable upload URL"):
        await service.start_resumable_upload(
            "nb_123",
            "report.pdf",
            12,
            "src_123",
            "application/pdf",
            expected_epoch=1,
        )


@pytest.mark.parametrize(
    ("status", "expected_exc"),
    [
        (400, ValidationError),  # unsupported file type (.pub) → clean 4xx
        (415, ValidationError),  # unsupported media type → clean 4xx
        (401, AuthError),  # session/credentials rejected → auth
        (403, AuthError),  # forbidden → auth
        (302, AuthError),  # session bounced to login → auth, NOT "unsupported file"
        (503, ServerError),  # transient upstream failure stays a (retriable) server error
        (429, RateLimitError),  # rate limit stays a rate-limit error
    ],
)
@pytest.mark.asyncio
async def test_start_resumable_upload_maps_upstream_status_to_notebooklm_error(
    status: int, expected_exc: type[Exception]
) -> None:
    # NotebookLM's /upload endpoint answers with a raw HTTP status (not a
    # batchexecute envelope), so the client layer must classify it — a raw
    # ``httpx.HTTPStatusError`` must never leak to callers (#1892). A 4xx
    # request/file rejection surfaces as ``ValidationError`` (the same bad-input
    # category as the local unsupported-type check) so the MCP/REST adapters
    # render a clean, redacted 4xx instead of an opaque 500.
    req = httpx.Request("POST", "https://notebooklm.google.com/upload/_/")
    response = httpx.Response(status, request=req, text="upstream rejected the upload")
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client_factory = MagicMock(return_value=client)
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)

    with pytest.raises(expected_exc) as exc_info:
        await service.start_resumable_upload(
            "nb_123",
            "newsletter.pub",
            12,
            "src_123",
            "application/vnd.ms-publisher",
            expected_epoch=1,
        )
    # The filename rides in the classified message; the raw httpx error is chained.
    assert "newsletter.pub" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


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
            expected_epoch=1,
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
    client.__aenter__.return_value = client
    client_factory = MagicMock(return_value=client)
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)
    logger = MagicMock()

    await service.upload_file_streaming(
        "https://notebooklm.google.com/upload/_/?upload_id=SECRET_UPLOAD_ID",
        file_path,
        logger=logger,
        expected_epoch=1,
    )

    debug_messages = [str(call) for call in logger.debug.call_args_list]
    assert all("SECRET_UPLOAD_ID" not in message for message in debug_messages)
    assert any(
        "https://notebooklm.google.com/upload/_/?..." in message for message in debug_messages
    )


def _mock_post_client() -> tuple[AsyncMock, MagicMock]:
    """Return ``(client, client_factory)`` whose ``post`` records its headers."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.headers = {}
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    return client, MagicMock(return_value=client)


# --- Origin/Referer must track the URL the bytes actually go to ------------
# Once either personal host may be named in ``X-Goog-Upload-URL``, an
# ``Origin`` copied from the configured base URL can name the *other* host —
# and Google's origin-bound auth checks reject such a POST. Every upload
# request therefore derives its ``Origin``/``Referer`` from the endpoint it is
# addressed to (for stream/cancel: the *validated* upload URL).


@pytest.mark.asyncio
async def test_start_resumable_upload_origin_tracks_the_upload_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebook.google.com")
    client, client_factory = _mock_post_client()
    # Scotty answers naming the *other* personal host: still accepted (A4).
    client.post.return_value.headers = {
        "x-goog-upload-url": "https://notebooklm.google.com/upload/_/?upload_id=session"
    }
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)

    upload_url = await service.start_resumable_upload(
        "nb_123",
        "report.pdf",
        12,
        "src_123",
        "application/pdf",
        expected_epoch=1,
    )

    assert upload_url == "https://notebooklm.google.com/upload/_/?upload_id=session"
    headers = client.post.await_args.kwargs["headers"]
    assert headers["Origin"] == "https://notebook.google.com"
    assert headers["Referer"] == "https://notebook.google.com/"


@pytest.mark.asyncio
async def test_upload_file_streaming_origin_tracks_validated_upload_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.google.com")
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"content")
    client, client_factory = _mock_post_client()
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)

    await service.upload_file_streaming(
        "https://notebook.google.com/upload/_/?upload_id=session",
        file_path,
        expected_epoch=1,
    )

    headers = client.post.await_args.kwargs["headers"]
    assert headers["Origin"] == "https://notebook.google.com"
    assert headers["Referer"] == "https://notebook.google.com/"


@pytest.mark.asyncio
async def test_cancel_upload_session_origin_tracks_validated_upload_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.google.com")
    client, client_factory = _mock_post_client()
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)

    await service.cancel_upload_session(
        "https://notebook.google.com/upload/_/?upload_id=session",
        "0",
        logger=MagicMock(),
        _expected_epoch=1,
    )

    headers = client.post.await_args.kwargs["headers"]
    assert headers["Origin"] == "https://notebook.google.com"
    assert headers["Referer"] == "https://notebook.google.com/"
    assert headers["x-goog-upload-command"] == "cancel"


@pytest.mark.asyncio
async def test_cancel_upload_session_builds_no_headers_before_validation() -> None:
    """An untrusted host must never reach an outbound header.

    ``cancel_upload_session`` used to build its header dict *above* the
    validation call, inside a broad ``except Exception``. Deriving ``Origin``
    from the upload URL in that order would have put a server-named, untrusted
    host into an outbound header, so the construction now sits below the guard.
    """
    client, client_factory = _mock_post_client()
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)

    await service.cancel_upload_session(
        "https://evil.example/upload/_/?upload_id=secret",
        "0",
        logger=MagicMock(),
        _expected_epoch=1,
    )

    client_factory.assert_not_called()
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_upload_session_redacts_credentials_on_validation_failure() -> None:
    client_factory = MagicMock()
    runtime = HttpRuntime()
    service = make_pipeline(kernel=runtime, auth=runtime, async_client_factory=client_factory)
    logger = MagicMock()

    await service.cancel_upload_session(
        "https://alice:s3cr3t@notebooklm.google.com/upload/_/?upload_id=SECRET_UPLOAD_ID",
        "0",
        logger=logger,
        _expected_epoch=1,
    )

    client_factory.assert_not_called()
    debug_messages = [str(call) for call in logger.debug.call_args_list]
    assert any(
        "https://notebooklm.google.com/upload/_/?..." in message for message in debug_messages
    )
    assert all("alice" not in message for message in debug_messages)
    assert all("s3cr3t" not in message for message in debug_messages)
    assert all("SECRET_UPLOAD_ID" not in message for message in debug_messages)


class _EpochKernel(HttpRuntime):
    """Cookie owner accepting whichever generation the test has activated."""

    def get_http_client(self, *, expected_epoch: int) -> HttpRuntime:
        assert expected_epoch > 0
        return self


class _BlockedEnterClient:
    def __init__(self, entered: asyncio.Event, attempts: list[str]) -> None:
        self._entered = entered
        self._attempts = attempts
        self._release = asyncio.Event()
        self.posts = 0

    async def __aenter__(self) -> _BlockedEnterClient:
        self._entered.set()
        await self._release.wait()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.aclose()

    async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
        self.posts += 1
        request = httpx.Request("POST", "https://notebooklm.google.com/upload/_/")
        return httpx.Response(200, request=request)

    async def aclose(self) -> None:
        self._attempts.append("old-client")
        self._release.set()


class _SuccessfulClient:
    def __init__(self, attempts: list[str]) -> None:
        self._attempts = attempts
        self.posts = 0

    async def __aenter__(self) -> _SuccessfulClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.aclose()

    async def post(self, url: str, **_kwargs: Any) -> httpx.Response:
        self.posts += 1
        return httpx.Response(200, request=httpx.Request("POST", url))

    async def aclose(self) -> None:
        self._attempts.append("new-client")


@pytest.mark.asyncio
async def test_forced_close_gathers_stream_body_and_cannot_cancel_or_touch_reopened_epoch() -> None:
    """Every old upload handle settles before epoch 2 is made visible."""
    loop = asyncio.get_running_loop()
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        max_concurrent_rpcs=None,
    )
    supervisor.set_bound_loop(loop)
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)

    entered = asyncio.Event()
    close_attempts: list[str] = []
    old_client = _BlockedEnterClient(entered, close_attempts)
    new_client = _SuccessfulClient(close_attempts)
    clients = iter((old_client, new_client))
    kernel = _EpochKernel()
    pipeline = SourceUploadPipeline(
        rpc=UploadRuntime(),  # type: ignore[arg-type]
        supervisor=supervisor,
        kernel=kernel,
        auth=kernel,
        async_client_factory=lambda **_kwargs: next(clients),  # type: ignore[arg-type]
    )
    await pipeline.open(loop, 1)
    old_body = io.BytesIO(b"old generation bytes")

    async def _old_upload() -> None:
        async with pipeline.transport_operation_scope("old-upload") as epoch:
            await pipeline.upload_file_streaming(
                "https://notebooklm.google.com/upload/_/?upload_id=old",
                old_body,
                total_bytes=len(old_body.getvalue()),
                expected_epoch=epoch,
            )

    old_operation = asyncio.create_task(_old_upload())
    await entered.wait()
    # The outer workflow and its finalize child are both uploader-owned.
    assert old_operation in pipeline._transport_tasks
    assert len(pipeline._transport_tasks) == 2

    await supervisor.begin_closing(1)
    await pipeline.prepare_close()
    await pipeline.close_resources()
    await asyncio.gather(old_operation, return_exceptions=True)
    supervisor.mark_closed(1)

    assert old_body.closed
    assert old_client.posts == 0
    assert close_attempts.count("old-client") >= 1
    assert pipeline._transport_tasks == set()
    assert pipeline._transport_clients == set()

    supervisor.reset_after_open()
    supervisor.prepare_generation(2)
    await pipeline.open(loop, 2)
    supervisor.start_accepting(2)
    new_body = io.BytesIO(b"new generation bytes")
    async with pipeline.transport_operation_scope("new-upload") as epoch:
        assert epoch == 2
        await pipeline.upload_file_streaming(
            "https://notebooklm.google.com/upload/_/?upload_id=new",
            new_body,
            total_bytes=len(new_body.getvalue()),
            expected_epoch=epoch,
        )

    assert old_client.posts == 0
    assert new_client.posts == 1
    assert new_body.closed


class _FailingCloseClient:
    def __init__(
        self,
        name: str,
        attempts: list[str],
        failure: BaseException | None,
    ) -> None:
        self._name = name
        self._attempts = attempts
        self._failure = failure

    async def aclose(self) -> None:
        self._attempts.append(self._name)
        failure, self._failure = self._failure, None
        if failure is not None:
            raise failure


@pytest.mark.asyncio
async def test_upload_teardown_attempts_all_tasks_and_clients_before_process_exit() -> None:
    """One cleanup failure or process exit cannot skip sibling resources."""
    pipeline = make_pipeline()
    attempts: list[str] = []
    task_started = asyncio.Event()

    async def _owned_task(name: str) -> None:
        task_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            attempts.append(name)

    first_task = asyncio.create_task(_owned_task("task-one"))
    second_task = asyncio.create_task(_owned_task("task-two"))
    await task_started.wait()
    pipeline._transport_tasks.update((first_task, second_task))
    pipeline._transport_clients.update(
        {
            _FailingCloseClient("ordinary-client", attempts, RuntimeError("close failed")),
            _FailingCloseClient("exit-client", attempts, SystemExit(19)),
            _FailingCloseClient("healthy-client", attempts, None),
        }  # type: ignore[arg-type]
    )

    with pytest.raises(SystemExit) as exc_info:
        await pipeline.prepare_close()

    assert exc_info.value.code == 19
    assert set(attempts) == {
        "task-one",
        "task-two",
        "ordinary-client",
        "exit-client",
        "healthy-client",
    }
    # Fail-once clients let the mandatory close_resources pass prove that it
    # retries and clears the complete registry even after prepare_close failed.
    await pipeline.close_resources()
    assert pipeline._transport_tasks == set()
    assert pipeline._transport_clients == set()
    assert pipeline._registry_lock is None


@pytest.mark.asyncio
async def test_pre_dispatch_cancellation_awaits_tracked_scotty_cancel_child() -> None:
    """Caller cancellation is not re-raised while Scotty cancel is still live."""
    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()
    finalize_gate = asyncio.Event()

    class DelayedSupervisor(UploadRuntime):
        async def spawn_child(self, label: str, factory: Any) -> asyncio.Task[Any]:
            async def _run() -> Any:
                if label.startswith("upload-finalize:"):
                    await finalize_gate.wait()
                return await factory()

            return asyncio.create_task(_run(), name=label)

    class CancelClient:
        async def __aenter__(self) -> CancelClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, url: str, **_kwargs: Any) -> httpx.Response:
            cancel_started.set()
            await release_cancel.wait()
            return httpx.Response(200, request=httpx.Request("POST", url))

        async def aclose(self) -> None:
            return None

    supervisor = DelayedSupervisor()
    kernel = _EpochKernel()
    pipeline = make_pipeline(
        session=supervisor,
        supervisor=supervisor,
        kernel=kernel,
        auth=kernel,
        async_client_factory=lambda **_kwargs: CancelClient(),  # type: ignore[arg-type]
    )
    body = io.BytesIO(b"not dispatched")
    operation = asyncio.create_task(
        pipeline.upload_file_streaming(
            "https://notebooklm.google.com/upload/_/?upload_id=cancel-me",
            body,
            total_bytes=len(body.getvalue()),
            expected_epoch=1,
        )
    )
    await asyncio.sleep(0)
    operation.cancel()
    await cancel_started.wait()

    assert not operation.done()
    assert any(task.get_name().startswith("upload-cancel:") for task in pipeline._transport_tasks)

    release_cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert body.closed
    assert pipeline._transport_tasks == set()


@pytest.mark.asyncio
async def test_upload_child_is_admitted_in_draining_and_rejected_in_closing() -> None:
    """A held workflow may finish nested I/O only until forced closing begins."""
    loop = asyncio.get_running_loop()
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        max_concurrent_rpcs=None,
    )
    supervisor.set_bound_loop(loop)
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    close_attempts: list[str] = []
    created: list[_SuccessfulClient] = []

    def _factory(**_kwargs: Any) -> _SuccessfulClient:
        client = _SuccessfulClient(close_attempts)
        created.append(client)
        return client

    kernel = _EpochKernel()
    pipeline = SourceUploadPipeline(
        rpc=UploadRuntime(),  # type: ignore[arg-type]
        supervisor=supervisor,
        kernel=kernel,
        auth=kernel,
        async_client_factory=_factory,  # type: ignore[arg-type]
    )
    await pipeline.open(loop, 1)

    async with pipeline.transport_operation_scope("held-upload") as epoch:
        await supervisor.stop_accepting(epoch)
        draining_body = io.BytesIO(b"finish while draining")
        await pipeline.upload_file_streaming(
            "https://notebooklm.google.com/upload/_/?upload_id=draining",
            draining_body,
            total_bytes=len(draining_body.getvalue()),
            expected_epoch=epoch,
        )
        assert draining_body.closed
        assert created[0].posts == 1

        await supervisor.begin_closing(epoch)
        closing_body = io.BytesIO(b"must not dispatch")
        with pytest.raises(RuntimeError, match="not accepting child work"):
            await pipeline.upload_file_streaming(
                "https://notebooklm.google.com/upload/_/?upload_id=closing",
                closing_body,
                total_bytes=len(closing_body.getvalue()),
                expected_epoch=epoch,
            )
        assert closing_body.closed
        assert len(created) == 1

    await pipeline.prepare_close()
    await pipeline.close_resources()
    supervisor.mark_closed(1)
