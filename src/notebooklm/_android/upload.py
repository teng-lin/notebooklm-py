"""Epoch-fenced Android PDF registration and Scotty upload transaction."""

from __future__ import annotations

import asyncio
import json
import math
import mimetypes
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any, Protocol, TypeVar, cast
from urllib.parse import urlsplit

from .._callbacks import maybe_await_callback
from .._deadline import RuntimeDeadline
from .._loop_affinity import assert_bound_loop
from .._loop_bound import LoopBoundPrimitive
from .._runtime.config import DEFAULT_MAX_CONCURRENT_UPLOADS, normalize_max_concurrent_uploads
from ..exceptions import (
    NetworkError,
    RPCError,
    SourceAddError,
    ValidationError,
)
from ..types import Source, SourceStatus
from .auth import BearerProvider
from .errors import sanitize_escaping_exception, unsupported_operation
from .evidence import ANDROID_EVIDENCE_PROFILE
from .proto.labs.language.tailwind.common.protos import metadata_pb2, provenance_pb2
from .session import AndroidSession

_T = TypeVar("_T")
_METADATA_PROTO = cast(Any, metadata_pb2)
_PROVENANCE_PROTO = cast(Any, provenance_pb2)

UPLOAD_ORIGIN = "https://notebooklm-pa.googleapis.com"
UPLOAD_PATH_PREFIX = "/upload/upload/"
PDF_MIME_TYPE = "application/pdf"
_SAFE_BEARER_HOSTS = frozenset({"notebooklm-pa.googleapis.com", "lh3.googleusercontent.com"})
_CHUNK_SIZE = 64 * 1024


class _RetiredEpochError(RuntimeError):
    """Private signal for lifecycle fencing, never an HTTP transport failure."""


class _ProgressCallbackError(Exception):
    """Carry a caller callback failure across the secret-bearing worker boundary."""

    def __init__(self, error: Exception) -> None:
        super().__init__("Android upload progress callback failed")
        self.error = error


class AndroidHTTPClientFactory(Protocol):
    """Construct one credential-free, redirect-disabled HTTP client."""

    def __call__(self, **kwargs: Any) -> Any: ...


RegisterTentative = Callable[[str, str, int, float], Awaitable[str]]
WaitForSource = Callable[[str, str, float, int], Awaitable[Source]]
RenameUploaded = Callable[[str, str, str, int], Awaitable[str | None]]
ProgressCallback = Callable[[int, int], object]


@dataclass(frozen=True)
class _HTTPOutcome:
    status_code: int
    upload_status: str | None
    session_url: str | None = None


@dataclass(frozen=True)
class _HTTPFailure:
    kind: str


@dataclass
class _UploadState:
    stage: str = "register"
    source_id: str | None = None


def android_provenance() -> Any:
    """Build the exact captured Android provenance message."""

    profile = ANDROID_EVIDENCE_PROFILE
    return _PROVENANCE_PROTO.Provenance(
        origin_product_type=_PROVENANCE_PROTO.Provenance.GOOGLE_NOTEBOOKLM,
        client_info=_PROVENANCE_PROTO.ClientInfo(
            application_platform=_PROVENANCE_PROTO.ClientInfo.NATIVE,
            device=_PROVENANCE_PROTO.ClientInfo.MOBILE_ANDROID,
            application_version=profile.app_version,
        ),
    )


def android_request_context() -> Any:
    """Build the exact captured Android request context."""

    return _METADATA_PROTO.RequestContext(
        client_type=_METADATA_PROTO.ANDROID_APP,
        client_metadata=_METADATA_PROTO.ClientMetadata(
            client_version=ANDROID_EVIDENCE_PROFILE.app_version
        ),
        provenance=android_provenance(),
    )


def build_upload_start_body(project_id: str, source_id: str) -> bytes:
    """Build canonical proto3 JSON explicitly, without a generic converter."""

    profile = ANDROID_EVIDENCE_PROFILE
    client_info = {
        "applicationPlatform": "NATIVE",
        "device": "MOBILE_ANDROID",
        "applicationVersion": profile.app_version,
    }
    provenance = {
        "originProductType": "GOOGLE_NOTEBOOKLM",
        "clientInfo": client_info,
    }
    body = {
        "projectId": project_id,
        "requestContext": {
            "clientType": "ANDROID_APP",
            "clientMetadata": {"clientVersion": profile.app_version},
            "provenance": provenance,
        },
        "sourceId": source_id,
        "provenance": provenance,
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _resolve_pdf_mime(path: Path, supplied: str | None) -> str:
    inferred = supplied if supplied is not None else mimetypes.guess_type(path.name)[0]
    if inferred is None or inferred.strip().lower() != PDF_MIME_TYPE:
        unsupported_operation("sources.add_file for non-PDF files")
    return PDF_MIME_TYPE


def _validate_project_id(project_id: str) -> None:
    if (
        not project_id
        or any(character in project_id for character in "/?#\\")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in project_id)
    ):
        raise ValidationError("Invalid notebook id for Android PDF upload")


def _one_header(headers: Mapping[str, str], name: str) -> str | None:
    get_list = getattr(headers, "get_list", None)
    if callable(get_list):
        values = list(get_list(name))
    else:
        value = headers.get(name)
        values = [] if value is None else [value]
    if len(values) != 1:
        return None
    value = str(next(iter(values)))
    if not value or "," in value or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    return value


def validate_upload_session_url(raw_url: str, project_id: str) -> str:
    """Validate one secret Scotty capability without normalizing or rebuilding it."""

    if (
        not raw_url
        or "," in raw_url
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw_url)
    ):
        raise ValidationError("Invalid Android upload session response")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        raise ValidationError("Invalid Android upload session response") from None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.hostname != "notebooklm-pa.googleapis.com"
        or port not in (None, 443)
        or parsed.netloc not in {"notebooklm-pa.googleapis.com", "notebooklm-pa.googleapis.com:443"}
        or parsed.path != f"{UPLOAD_PATH_PREFIX}{project_id}"
    ):
        raise ValidationError("Invalid Android upload session response")
    parts = parsed.query.split("&")
    if len(parts) != 2 or any(part.count("=") != 1 for part in parts):
        raise ValidationError("Invalid Android upload session response")
    pairs = [part.split("=", 1) for part in parts]
    if {key for key, _ in pairs} != {"upload_id", "upload_protocol"}:
        raise ValidationError("Invalid Android upload session response")
    values = dict(pairs)
    if not values["upload_id"] or values["upload_protocol"] != "resumable":
        raise ValidationError("Invalid Android upload session response")
    return raw_url


def _upload_failure(filename: str, state: _UploadState, detail: str) -> SourceAddError:
    error = SourceAddError(
        filename,
        message=f"Android PDF upload failed during {state.stage}: {detail}.",
    )
    error.cause = None
    cast(Any, error).stage = state.stage
    if state.source_id is not None:
        cast(Any, error).source_id = state.source_id
    return error


async def _bounded(awaitable: Awaitable[_T], deadline: RuntimeDeadline) -> _T:
    remaining = deadline.remaining()
    if remaining <= 0.0:
        if hasattr(awaitable, "close"):
            cast(Any, awaitable).close()
        raise TimeoutError
    return await asyncio.wait_for(awaitable, timeout=remaining)


class AndroidUploadPipeline(LoopBoundPrimitive):
    """Own one private Android control-plane/data-plane upload transaction."""

    name = "android-upload"

    def __init__(
        self,
        *,
        session: AndroidSession,
        bearer_provider: BearerProvider,
        upload_timeout: float = 300.0,
        max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
        record_upload_queue_wait: Callable[[float], None] | None = None,
        async_client_factory: AndroidHTTPClientFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(float(upload_timeout)) or float(upload_timeout) <= 0.0:
            raise ValueError("upload_timeout must be a finite positive number")
        self._transport = session
        self._bearer_provider = bearer_provider
        self._upload_timeout = float(upload_timeout)
        self._max_concurrent_uploads = normalize_max_concurrent_uploads(max_concurrent_uploads)
        self._record_upload_queue_wait = record_upload_queue_wait
        self._async_client_factory = async_client_factory
        self._monotonic = monotonic
        self._active_epoch: int | None = None
        self._closing = True
        self._upload_semaphore: asyncio.Semaphore | None = None
        self._transport_tasks: set[asyncio.Task[Any]] = set()
        self._transport_clients: set[Any] = set()
        self._open_files: set[IO[bytes]] = set()

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        self._upload_semaphore = None

    def reset_after_open(self) -> None:
        self._upload_semaphore = None
        self._transport_tasks.clear()
        self._transport_clients.clear()
        self._open_files.clear()

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        if self._bound_loop is not loop:
            raise RuntimeError("Android upload transport was not bound by the client lifecycle.")
        assert_bound_loop(self._bound_loop)
        self._active_epoch = epoch
        self._closing = False

    async def prepare_close(self) -> None:
        if self._bound_loop is not None:
            assert_bound_loop(self._bound_loop)
        self._closing = True
        self._active_epoch = None
        await self._settle_resources()

    async def close_resources(self) -> None:
        self._closing = True
        self._active_epoch = None
        try:
            await self._settle_resources()
        finally:
            self._transport_tasks.clear()
            self._transport_clients.clear()
            self._open_files.clear()
            self._upload_semaphore = None

    async def _settle_resources(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._transport_tasks if task is not current]
        clients = list(self._transport_clients)
        files = list(self._open_files)
        for task in tasks:
            task.cancel()
        for client in clients:
            close = getattr(client, "aclose", None)
            if callable(close):
                try:
                    await close()
                except BaseException:
                    pass
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for file_obj in files:
            try:
                file_obj.close()
            except BaseException:
                pass

    def _assert_epoch(self, expected_epoch: int) -> None:
        assert_bound_loop(self._bound_loop)
        if self._closing or self._active_epoch != expected_epoch:
            raise _RetiredEpochError(
                "Android upload belongs to a retired resource generation "
                f"(expected={expected_epoch}, active={self._active_epoch})."
            )
        try:
            self._transport.assert_epoch(expected_epoch)
        except RuntimeError:
            raise _RetiredEpochError("Android upload belongs to a retired resource generation") from None

    def _client_factory(self) -> AndroidHTTPClientFactory:
        if self._async_client_factory is not None:
            return self._async_client_factory
        from .._curl_cffi_transport import resolve_transport_factory

        return cast(AndroidHTTPClientFactory, resolve_transport_factory())

    def _upload_slot(self) -> asyncio.Semaphore:
        assert_bound_loop(self._bound_loop)
        if self._upload_semaphore is None:
            self._upload_semaphore = asyncio.Semaphore(self._max_concurrent_uploads)
        return self._upload_semaphore

    async def upload_pdf(
        self,
        notebook_id: str,
        file_path: str | Path,
        mime_type: str | None,
        *,
        wait: bool,
        wait_timeout: float,
        title: str | None,
        on_progress: ProgressCallback | None,
        register_tentative: RegisterTentative,
        wait_until_registered: WaitForSource,
        wait_until_ready: WaitForSource,
        rename_uploaded: RenameUploaded,
    ) -> Source:
        """Upload one PDF without retaining this secret owner in failures."""

        pipeline = self
        result: Source | None = None
        failure: BaseException | None = None
        try:
            result = await pipeline._upload_pdf_impl(
                notebook_id,
                file_path,
                mime_type,
                wait=wait,
                wait_timeout=wait_timeout,
                title=title,
                on_progress=on_progress,
                register_tentative=register_tentative,
                wait_until_registered=wait_until_registered,
                wait_until_ready=wait_until_ready,
                rename_uploaded=rename_uploaded,
            )
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            del self, pipeline
        if failure is not None:
            raise failure
        return cast(Source, result)

    async def _upload_pdf_impl(
        self,
        notebook_id: str,
        file_path: str | Path,
        mime_type: str | None,
        *,
        wait: bool,
        wait_timeout: float,
        title: str | None,
        on_progress: ProgressCallback | None,
        register_tentative: RegisterTentative,
        wait_until_registered: WaitForSource,
        wait_until_ready: WaitForSource,
        rename_uploaded: RenameUploaded,
    ) -> Source:
        raw_path = Path(file_path)
        _resolve_pdf_mime(raw_path, mime_type)
        _validate_project_id(notebook_id)
        requested_title = None
        if title is not None:
            requested_title = title.strip()
            if not requested_title:
                raise ValidationError("Title cannot be empty or whitespace-only")

        deadline = RuntimeDeadline.start(self._upload_timeout, monotonic=self._monotonic)
        state = _UploadState()
        scope = self._transport.operation_scope("Android source upload")
        lease = await _bounded(scope.__aenter__(), deadline)
        try:
            try:
                source_id, filename = await _bounded(
                    self._control_plane(
                        notebook_id,
                        raw_path,
                        deadline,
                        state,
                        on_progress,
                        lease.epoch,
                        register_tentative,
                    ),
                    deadline,
                )
            except TimeoutError:
                raise _upload_failure(filename=raw_path.name, state=state, detail="timed out") from None

            needs_rename = requested_title is not None and requested_title != filename
            if wait:
                source = await wait_until_ready(notebook_id, source_id, wait_timeout, lease.epoch)
            elif needs_rename:
                source = await wait_until_registered(
                    notebook_id, source_id, wait_timeout, lease.epoch
                )
            else:
                source = Source(
                    id=source_id,
                    title=filename,
                    status=SourceStatus.PROCESSING,
                    _type_code=None,
                )

            if needs_rename:
                assert requested_title is not None
                try:
                    echoed_title = await rename_uploaded(
                        notebook_id,
                        source_id,
                        requested_title,
                        lease.epoch,
                    )
                    source = replace(source, title=echoed_title or requested_title)
                except (RPCError, NetworkError):
                    # Fixed, capability-free diagnostic: never log title, URL or token.
                    import logging

                    logging.getLogger(__name__).warning(
                        "Android PDF source %s uploaded but title finalization failed",
                        source_id,
                    )
            return source
        finally:
            await scope.__aexit__(None, None, None)

    async def _control_plane(
        self,
        notebook_id: str,
        raw_path: Path,
        deadline: RuntimeDeadline,
        state: _UploadState,
        on_progress: ProgressCallback | None,
        expected_epoch: int,
        register_tentative: RegisterTentative,
    ) -> tuple[str, str]:
        self._assert_epoch(expected_epoch)

        def _resolve_and_check() -> Path:
            resolved = raw_path.resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"File not found: {resolved}")
            if not resolved.is_file():
                raise ValidationError(f"Not a regular file: {resolved}")
            return resolved

        resolved = await _bounded(asyncio.to_thread(_resolve_and_check), deadline)
        filename = resolved.name
        semaphore = self._upload_slot()
        queued_at = self._monotonic()
        await _bounded(semaphore.acquire(), deadline)
        if self._record_upload_queue_wait is not None:
            self._record_upload_queue_wait(self._monotonic() - queued_at)
        file_obj: IO[bytes] | None = None
        try:
            def _open_and_stat() -> tuple[IO[bytes], int]:
                handle = open(resolved, "rb")  # noqa: SIM115
                try:
                    return handle, os.fstat(handle.fileno()).st_size
                except BaseException:
                    handle.close()
                    raise

            file_obj, file_size = await _bounded(asyncio.to_thread(_open_and_stat), deadline)
            assert file_obj is not None
            self._open_files.add(file_obj)
            self._assert_epoch(expected_epoch)
            state.stage = "register"
            source_id = await register_tentative(
                notebook_id,
                filename,
                expected_epoch,
                deadline.remaining(),
            )
            state.source_id = source_id

            state.stage = "start"
            start = await self._run_http_child(
                self._start_worker(
                    notebook_id,
                    source_id,
                    filename,
                    file_size,
                    expected_epoch,
                    deadline,
                ),
                expected_epoch,
            )
            if isinstance(start, _HTTPFailure):
                raise _upload_failure(filename, state, "request failed")
            if start.status_code != 200 or start.upload_status != "active":
                raise _upload_failure(filename, state, f"HTTP status {start.status_code}")
            if start.session_url is None:
                raise _upload_failure(filename, state, "invalid session response")
            try:
                session_url = validate_upload_session_url(start.session_url, notebook_id)
            except ValidationError:
                raise _upload_failure(filename, state, "invalid session response") from None

            state.stage = "finalize"
            assert file_obj is not None
            final = await self._run_http_child(
                self._finalize_worker(
                    session_url,
                    file_obj,
                    file_size,
                    on_progress,
                    expected_epoch,
                    deadline,
                ),
                expected_epoch,
            )
            if isinstance(final, _HTTPFailure):
                raise _upload_failure(filename, state, "request failed")
            if final.status_code != 200 or final.upload_status != "final":
                raise _upload_failure(filename, state, f"HTTP status {final.status_code}")
            return source_id, filename
        finally:
            if file_obj is not None:
                self._open_files.discard(file_obj)
                file_obj.close()
            semaphore.release()

    async def _run_http_child(
        self,
        awaitable: Awaitable[_HTTPOutcome | _HTTPFailure],
        expected_epoch: int,
    ) -> _HTTPOutcome | _HTTPFailure:
        self._assert_epoch(expected_epoch)
        async def _child() -> _HTTPOutcome | _HTTPFailure:
            return await awaitable

        task: asyncio.Task[_HTTPOutcome | _HTTPFailure] = asyncio.create_task(
            _child(), name="notebooklm-android-upload-http"
        )
        self._transport_tasks.add(task)
        task.add_done_callback(self._transport_tasks.discard)
        try:
            return await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self._closing or self._active_epoch != expected_epoch:
                raise RuntimeError("Android upload was interrupted by transport close.") from None
            raise

    async def _bearer_for(self, url: str, expected_epoch: int, deadline: RuntimeDeadline) -> Any:
        self._assert_epoch(expected_epoch)
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            raise RuntimeError("Android bearer destination is invalid") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _SAFE_BEARER_HOSTS
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise RuntimeError("Android bearer destination is not allowlisted")
        try:
            credential = await _bounded(self._bearer_provider.get(expected_epoch), deadline)
        except RuntimeError:
            self._assert_epoch(expected_epoch)
            raise
        self._assert_epoch(expected_epoch)
        return credential

    async def _start_worker(
        self,
        notebook_id: str,
        source_id: str,
        filename: str,
        file_size: int,
        expected_epoch: int,
        deadline: RuntimeDeadline,
    ) -> _HTTPOutcome | _HTTPFailure:
        url = f"{UPLOAD_ORIGIN}{UPLOAD_PATH_PREFIX}{notebook_id}"
        credential: Any | None = None
        client: Any | None = None
        response: Any | None = None
        try:
            credential = await self._bearer_for(url, expected_epoch, deadline)
            headers = {
                "Authorization": f"Bearer {credential.token}",
                "Content-Type": "text/plain; charset=utf-8",
                "User-Agent": ANDROID_EVIDENCE_PROFILE.app_user_agent,
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-File-Name": filename,
                "X-Goog-Upload-Header-Content-Length": str(file_size),
                "X-Goog-Upload-Header-Content-Type": PDF_MIME_TYPE,
                "X-Goog-Upload-Protocol": "resumable",
            }
            client = self._client_factory()(
                cookies=None,
                follow_redirects=False,
                timeout=deadline.remaining(),
            )
            self._assert_epoch(expected_epoch)
            self._transport_clients.add(client)
            async with client:
                response = await _bounded(
                    client.post(
                        url,
                        headers=headers,
                        content=build_upload_start_body(notebook_id, source_id),
                        follow_redirects=False,
                    ),
                    deadline,
                )
                assert response is not None
                status_code = int(response.status_code)
                upload_status = _one_header(response.headers, "x-goog-upload-status")
                session_url = _one_header(response.headers, "x-goog-upload-url")
                if status_code == 401:
                    self._bearer_provider.invalidate(credential.generation)
                return _HTTPOutcome(status_code, upload_status, session_url)
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except _RetiredEpochError:
            raise
        except BaseException:
            return _HTTPFailure("transport")
        finally:
            if response is not None:
                close = getattr(response, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except BaseException:
                        pass
            if client is not None:
                self._transport_clients.discard(client)
            del credential, response, client

    async def _finalize_worker(
        self,
        session_url: str,
        file_obj: IO[bytes],
        file_size: int,
        on_progress: ProgressCallback | None,
        expected_epoch: int,
        deadline: RuntimeDeadline,
    ) -> _HTTPOutcome | _HTTPFailure:
        credential: Any | None = None
        client: Any | None = None
        response: Any | None = None
        try:
            credential = await self._bearer_for(session_url, expected_epoch, deadline)
            headers = {
                "Authorization": f"Bearer {credential.token}",
                "User-Agent": ANDROID_EVIDENCE_PROFILE.finalize_user_agent,
                "Content-Length": str(file_size),
                "X-Goog-Upload-Command": "upload, finalize",
                "X-Goog-Upload-Offset": "0",
            }
            client = self._client_factory()(
                cookies=None,
                follow_redirects=False,
                timeout=deadline.remaining(),
            )
            self._assert_epoch(expected_epoch)
            self._transport_clients.add(client)

            sent = 0

            async def _progress_chunk(length: int) -> None:
                nonlocal sent
                sent += length
                if on_progress is not None:
                    try:
                        await maybe_await_callback(on_progress, sent, file_size)
                    except asyncio.CancelledError:
                        raise
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception as error:
                        raise _ProgressCallbackError(error) from None

            async def _stream() -> Any:
                while chunk := await asyncio.to_thread(file_obj.read, _CHUNK_SIZE):
                    yield chunk
                    await _progress_chunk(len(chunk))

            async with client:
                from .._curl_cffi_transport import CurlCffiAsyncClient

                if isinstance(client, CurlCffiAsyncClient):
                    response = await _bounded(
                        client.stream_upload(
                            session_url,
                            file_obj,
                            total_bytes=file_size,
                            headers=headers,
                            method="PUT",
                            on_chunk=_progress_chunk,
                            overall_timeout=deadline.remaining(),
                            stop_on_cancel=True,
                        ),
                        deadline,
                    )
                else:
                    response = await _bounded(
                        client.put(
                            session_url,
                            headers=headers,
                            content=_stream(),
                            follow_redirects=False,
                        ),
                        deadline,
                    )
                assert response is not None
                status_code = int(response.status_code)
                upload_status = _one_header(response.headers, "x-goog-upload-status")
                if status_code == 401:
                    self._bearer_provider.invalidate(credential.generation)
                return _HTTPOutcome(status_code, upload_status)
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except _ProgressCallbackError as callback_failure:
            raise callback_failure.error from None
        except _RetiredEpochError:
            raise
        except BaseException:
            return _HTTPFailure("transport")
        finally:
            if response is not None:
                close = getattr(response, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except BaseException:
                        pass
            if client is not None:
                self._transport_clients.discard(client)
            del credential, response, client


__all__ = [
    "AndroidUploadPipeline",
    "android_provenance",
    "android_request_context",
    "build_upload_start_body",
    "validate_upload_session_url",
]
