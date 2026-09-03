"""Epoch-fenced Android file registration and Scotty upload transaction."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import mimetypes
import os
import shutil
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import quote, urlencode, urlsplit

import httpx

from .._callbacks import maybe_await_callback
from .._deadline import RuntimeDeadline, await_with_deadline
from .._loop_affinity import assert_bound_loop
from .._loop_bound import EpochFenced
from .._runtime.config import DEFAULT_MAX_CONCURRENT_UPLOADS, normalize_max_concurrent_uploads
from .._runtime.helpers import map_google_http_status
from .._source.drive import DriveRef, parse_drive_ref
from .._types.sources import _HTML_FILE_EXTENSIONS, _UPLOAD_FILE_EXTENSIONS
from ..exceptions import (
    AuthError,
    NetworkError,
    SourceAddError,
    ValidationError,
)
from ..types import Source
from .auth import BearerProvider
from .drive_staging import DriveStagingTransfer, ImportDriveFile
from .errors import sanitize_escaping_exception
from .evidence import ANDROID_EVIDENCE_PROFILE
from .session import AndroidSession

if TYPE_CHECKING:
    from .._sources import _UploadedSourceFinalizer


def _metadata_proto() -> Any:
    from .proto.labs.language.tailwind.common.protos import metadata_pb2

    return cast(Any, metadata_pb2)


def _provenance_proto() -> Any:
    from .proto.labs.language.tailwind.common.protos import provenance_pb2

    return cast(Any, provenance_pb2)


logger = logging.getLogger(__name__)

UPLOAD_ORIGIN = "https://notebooklm-pa.googleapis.com"
UPLOAD_PATH_PREFIX = "/upload/upload/"
_SAFE_BEARER_HOSTS = frozenset({"notebooklm-pa.googleapis.com", "lh3.googleusercontent.com"})
_CHUNK_SIZE = 64 * 1024
_HTML_UPLOAD_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
#: Extension -> content type for every extension in the public upload set.
#:
#: ``mimetypes.guess_type`` is consulted first but is **platform-dependent**: on
#: Windows it reads the registry and returns nothing for ``.docx``/``.pptx``, so
#: the resolver fell through to ``application/octet-stream`` there. That matters
#: beyond cosmetics on the Drive-staged path, where the resolved type becomes
#: the staged Drive file's declared ``mimeType`` and therefore decides how the
#: backend parses it. Pinning the whole supported set makes the upload wire
#: identical on every platform.
_EXTENSION_CONTENT_TYPES = {
    ".csv": "text/csv",
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".epub": "application/epub+zip",
    ".markdown": "text/markdown",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".txt": "text/plain",
}
_DRIVE_API_ORIGIN = "https://www.googleapis.com"
_DRIVE_NATIVE_MIME_PREFIX = "application/vnd.google-apps."
_MAX_DRIVE_DOWNLOAD_BYTES = 200 * 1024 * 1024
_DRIVE_STREAM_CHUNK_BYTES = 64 * 1024
_UPLOAD_SUPPORTED_EXTENSIONS = frozenset(_UPLOAD_FILE_EXTENSIONS)


def _set_private_temp_permissions(path: Path, mode: int) -> None:
    """Apply private POSIX bits while preserving inherited Windows ACLs.

    Windows Python 3.12 exposes only the DOS read-only bit through
    :func:`os.chmod`; a writable file still reports ``0o666`` and a directory
    ``0o777`` through ``stat``. It cannot express the owner-only contract
    represented by ``0o600``/``0o700``. The unique temporary directory and its
    exclusively-created child therefore inherit the user's Windows temp ACLs,
    matching the repository's credential-storage policy on that platform.
    """
    if sys.platform != "win32":
        os.chmod(path, mode)


def _resolve_upload_content_type(file_path: Path, mime_type: str | None) -> str:
    """Mirror the backend-neutral public upload MIME policy.

    The pinned table wins over :func:`mimetypes.guess_type` for the supported
    extensions so the resolved type does not vary by platform; see
    ``_EXTENSION_CONTENT_TYPES``.
    """

    if mime_type is not None:
        content_type = mime_type.strip()
        if not content_type:
            raise ValidationError("mime_type cannot be empty or whitespace-only")
        return content_type
    pinned = _EXTENSION_CONTENT_TYPES.get(file_path.suffix.lower())
    if pinned is not None:
        return pinned
    guessed, _encoding = mimetypes.guess_type(file_path.name)
    if guessed:
        return guessed
    return "application/octet-stream"


def _validate_upload_file_supported(file_path: Path, content_type: str) -> None:
    """Reject the same HTML-family uploads as the public Web upload path."""

    normalized = content_type.split(";", 1)[0].strip().lower()
    if (
        file_path.suffix.lower() in _HTML_FILE_EXTENSIONS
        or normalized in _HTML_UPLOAD_CONTENT_TYPES
    ):
        raise ValidationError(
            "HTML file uploads are not supported by NotebookLM's upload endpoint: "
            f"{file_path.name}. Convert the page to .txt, .md, or .pdf first, then retry."
        )


def _drive_resource_key_headers(ref: DriveRef) -> dict[str, str]:
    if ref.resource_key is None:
        return {}
    return {"X-Goog-Drive-Resource-Keys": f"{ref.file_id}/{ref.resource_key}"}


def _drive_filename(value: Any, file_id: str) -> str:
    """Return one safe leaf filename while retaining the evidenced extension."""

    raw = value if isinstance(value, str) else ""
    leaf = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    leaf = "".join(
        "_" if ord(character) < 0x20 or ord(character) == 0x7F else character for character in leaf
    )
    if leaf in {"", ".", ".."}:
        raise ValidationError(f"Drive did not return a usable filename for {file_id}.")
    if len(leaf.encode("utf-8")) > 240:
        raise ValidationError(f"Drive filename for {file_id} is too long to import safely.")
    return leaf


def _validate_drive_metadata(metadata: Any, ref: DriveRef) -> tuple[str, str]:
    if not isinstance(metadata, dict):
        raise ValidationError(f"Drive returned malformed metadata for {ref.file_id}.")
    filename = _drive_filename(metadata.get("name"), ref.file_id)
    mime_type = metadata.get("mimeType")
    if not isinstance(mime_type, str) or not mime_type:
        raise ValidationError(f"Drive did not return a MIME type for {ref.file_id}.")
    if mime_type.startswith(_DRIVE_NATIVE_MIME_PREFIX):
        raise ValidationError(
            f"Drive file {filename!r} is a native Google document. Add it with "
            "sources.add_drive(...) instead of sources.add_drive_file(...)."
        )
    capabilities = metadata.get("capabilities")
    if capabilities is not None and not isinstance(capabilities, dict):
        raise ValidationError(f"Drive returned malformed metadata for {ref.file_id}.")
    if isinstance(capabilities, dict) and capabilities.get("canDownload") is False:
        raise ValidationError(f"Drive file {filename!r} is not downloadable by this account.")
    extension = Path(filename).suffix.lower()
    if extension in _HTML_FILE_EXTENSIONS:
        raise ValidationError(
            "HTML isn't supported by NotebookLM upload; convert the page to "
            ".txt, .md, or .pdf first, then retry."
        )
    if extension not in _UPLOAD_SUPPORTED_EXTENSIONS:
        accepted = ", ".join(sorted(value.lstrip(".") for value in _UPLOAD_SUPPORTED_EXTENSIONS))
        raise ValidationError(
            f"Drive file {filename!r} has an unsupported type for NotebookLM upload. "
            f"Accepted: {accepted}."
        )
    size = metadata.get("size")
    if size is not None:
        try:
            declared_size = int(size)
        except (TypeError, ValueError):
            declared_size = -1
        if declared_size > _MAX_DRIVE_DOWNLOAD_BYTES:
            raise ValidationError(
                f"Drive file {ref.file_id} is {declared_size} bytes, over the 200 MiB download cap."
            )
    return filename, mime_type


def _resolve_upload_timeouts(
    configured: httpx.Timeout | float | None,
) -> tuple[float, httpx.Timeout | None]:
    """Resolve one public upload timeout into aggregate and per-request budgets.

    ``httpx.Timeout`` is preserved wholesale for both HTTP legs, matching the
    Web uploader's public contract.  The aggregate lifecycle fence is deliberately
    wider than either leg so registration, queueing, and finalization can complete
    without silently replacing the caller's component-specific values.
    """

    if configured is None:
        return 300.0, None
    if isinstance(configured, httpx.Timeout):
        components = [
            component
            for component in (
                configured.connect,
                configured.read,
                configured.write,
                configured.pool,
            )
            if component is not None
        ]
        for component in components:
            if not math.isfinite(float(component)) or float(component) <= 0.0:
                raise ValueError("upload_timeout components must be finite positive numbers")
        # A fully-unbounded httpx timeout remains unbounded at each HTTP request;
        # retain the historical 300s lifecycle fence for the surrounding control
        # plane rather than manufacturing arbitrary component values.
        aggregate = 300.0 if not components else max(300.0, 2.0 * sum(components))
        return aggregate, configured
    numeric = float(configured)
    if not math.isfinite(numeric) or numeric <= 0.0:
        raise ValueError("upload_timeout must be a finite positive number")
    return numeric, None


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
    return _provenance_proto().Provenance(
        origin_product_type=_provenance_proto().Provenance.GOOGLE_NOTEBOOKLM,
        client_info=_provenance_proto().ClientInfo(
            application_platform=_provenance_proto().ClientInfo.NATIVE,
            device=_provenance_proto().ClientInfo.MOBILE_ANDROID,
            application_version=profile.app_version,
        ),
    )


def android_request_context() -> Any:
    """Build the exact captured Android request context."""

    return _metadata_proto().RequestContext(
        client_type=_metadata_proto().ANDROID_APP,
        client_metadata=_metadata_proto().ClientMetadata(
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


def _validate_project_id(project_id: str) -> None:
    if (
        not project_id
        or any(character in project_id for character in "/?#\\")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in project_id)
    ):
        raise ValidationError("Invalid notebook id for Android file upload")


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
        message=f"Android file upload failed during {state.stage}: {detail}.",
    )
    error.cause = None
    cast(Any, error).stage = state.stage
    if state.source_id is not None:
        cast(Any, error).source_id = state.source_id
    return error


async def _settle_context_exit(
    context_manager: Any,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    traceback: Any | None,
) -> None:
    """Run one async-context exit to completion despite repeated cancellation."""

    exit_task = asyncio.create_task(context_manager.__aexit__(exc_type, exc, traceback))
    cancelled: asyncio.CancelledError | None = None
    exit_error: BaseException | None = None
    while True:
        try:
            await asyncio.shield(exit_task)
            break
        except asyncio.CancelledError as error:
            if exit_task.done():
                try:
                    exit_task.result()
                except asyncio.CancelledError as exit_cancelled:
                    exit_error = exit_cancelled
                except BaseException as settled_error:
                    exit_error = settled_error
                else:
                    cancelled = error
                break
            if cancelled is None:
                cancelled = error
        except BaseException as error:
            exit_error = error
            break
    if isinstance(exit_error, (KeyboardInterrupt, SystemExit)):
        raise exit_error
    if cancelled is not None:
        raise cancelled
    if exit_error is not None:
        raise exit_error


class AndroidUploadPipeline(EpochFenced):
    """Own one private Android control-plane/data-plane upload transaction."""

    name = "android-upload"

    def __init__(
        self,
        *,
        session: AndroidSession,
        bearer_provider: BearerProvider,
        upload_timeout: httpx.Timeout | float | None = None,
        max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
        record_upload_queue_wait: Callable[[float], None] | None = None,
        async_client_factory: AndroidHTTPClientFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            "Android upload belongs to a retired resource generation",
            error_type=_RetiredEpochError,
            initially_closing=True,
            assert_loop=True,
        )
        aggregate_timeout, http_timeout = _resolve_upload_timeouts(upload_timeout)
        self._transport = session
        self._bearer_provider = bearer_provider
        self._upload_timeout = aggregate_timeout
        self._http_timeout = http_timeout
        self._max_concurrent_uploads = normalize_max_concurrent_uploads(max_concurrent_uploads)
        self._record_upload_queue_wait = record_upload_queue_wait
        self._async_client_factory = async_client_factory
        self._monotonic = monotonic
        self._upload_semaphore: asyncio.Semaphore | None = None
        self._download_semaphore: asyncio.Semaphore | None = None
        self._transport_tasks: set[asyncio.Task[Any]] = set()
        self._transport_clients: set[Any] = set()
        self._open_files: set[IO[bytes]] = set()

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        self._upload_semaphore = None
        self._download_semaphore = None

    def reset_after_open(self) -> None:
        self._upload_semaphore = None
        self._download_semaphore = None
        self._transport_tasks.clear()
        self._transport_clients.clear()
        self._open_files.clear()

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        if self._bound_loop is not loop:
            raise RuntimeError("Android upload transport was not bound by the client lifecycle.")
        assert_bound_loop(self._bound_loop)
        self.activate(epoch)

    async def prepare_close(self) -> None:
        if self._bound_loop is not None:
            assert_bound_loop(self._bound_loop)
        self.fence()
        await self._settle_resources()

    async def close_resources(self) -> None:
        self.fence()
        try:
            await self._settle_resources()
        finally:
            self._transport_tasks.clear()
            self._transport_clients.clear()
            self._open_files.clear()
            self._upload_semaphore = None
            self._download_semaphore = None

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
        self.assert_epoch(expected_epoch)
        try:
            self._transport.assert_epoch(expected_epoch)
        except RuntimeError:
            raise _RetiredEpochError(
                "Android upload belongs to a retired resource generation"
            ) from None

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

    def _download_slot(self) -> asyncio.Semaphore:
        """Keep Drive downloads independent from upload admission to avoid deadlock."""

        assert_bound_loop(self._bound_loop)
        if self._download_semaphore is None:
            self._download_semaphore = asyncio.Semaphore(self._max_concurrent_uploads)
        return self._download_semaphore

    @staticmethod
    def _drive_url(file_id: str, *, media: bool) -> str:
        fields = "id,name,mimeType,size,capabilities(canDownload)"
        query = {"supportsAllDrives": "true"}
        if media:
            query["alt"] = "media"
        else:
            query["fields"] = fields
        return f"{_DRIVE_API_ORIGIN}/drive/v3/files/{quote(file_id, safe='')}?{urlencode(query)}"

    @staticmethod
    def _map_drive_status(response: Any, ref: DriveRef) -> None:
        status = int(response if type(response) is int else response.status_code)
        map_google_http_status(
            response,
            filename=f"fetching Drive file {ref.file_id}",
            chain=False,
        )
        if status >= 300:
            raise ValidationError(
                f"Drive returned HTTP {status} for {ref.file_id}; confirm the file id and "
                "that the selected account can download it."
            )

    async def _drive_metadata(
        self,
        client: Any,
        ref: DriveRef,
        credential: Any,
        deadline: RuntimeDeadline,
    ) -> tuple[str, str]:
        headers = {
            "Authorization": f"Bearer {credential.token}",
            **_drive_resource_key_headers(ref),
        }
        response: Any | None = None
        try:
            response = await await_with_deadline(
                client.get(
                    self._drive_url(ref.file_id, media=False),
                    headers=headers,
                    follow_redirects=False,
                ),
                deadline,
                on_timeout=TimeoutError,
            )
            assert response is not None
            status = int(response.status_code)
            if status == 401:
                self._bearer_provider.invalidate(credential.generation)
            self._map_drive_status(response, ref)
            try:
                metadata = response.json()
            except (TypeError, ValueError):
                raise ValidationError(
                    f"Drive returned malformed metadata for {ref.file_id}."
                ) from None
            return _validate_drive_metadata(metadata, ref)
        finally:
            if response is not None:
                close = getattr(response, "aclose", None)
                if callable(close):
                    await close()

    async def _stream_drive_media(
        self,
        client: Any,
        ref: DriveRef,
        filename: str,
        credential: Any,
        destination: Path,
        deadline: RuntimeDeadline,
    ) -> None:
        headers = {
            "Authorization": f"Bearer {credential.token}",
            **_drive_resource_key_headers(ref),
        }
        total = 0
        with destination.open("xb") as handle:
            _set_private_temp_permissions(destination, 0o600)
            self._open_files.add(handle)
            response_cm: Any | None = None
            response: Any | None = None
            response_entered = False
            exit_args: tuple[type[BaseException] | None, BaseException | None, Any | None] = (
                None,
                None,
                None,
            )
            try:
                response_cm = client.stream(
                    "GET",
                    self._drive_url(ref.file_id, media=True),
                    headers=headers,
                    follow_redirects=False,
                )
                # ``httpx.Timeout(None)`` deliberately disables HTTPX's
                # component timers.  Keep stream entry (including the header
                # wait) inside our independent 300s aggregate lifecycle fence.
                try:
                    response = await await_with_deadline(
                        response_cm.__aenter__(), deadline, on_timeout=TimeoutError
                    )
                    assert response is not None
                    response_entered = True
                    status = int(response.status_code)
                    if status == 401:
                        self._bearer_provider.invalidate(credential.generation)
                    self._map_drive_status(response, ref)
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            declared_size = int(declared)
                        except ValueError:
                            declared_size = -1
                        if declared_size > _MAX_DRIVE_DOWNLOAD_BYTES:
                            raise ValidationError(
                                f"Drive file {ref.file_id} is {declared_size} bytes, over the "
                                "200 MiB download cap."
                            )
                    iterator = response.aiter_bytes()
                    while True:
                        try:
                            chunk = await await_with_deadline(
                                anext(iterator), deadline, on_timeout=TimeoutError
                            )
                        except StopAsyncIteration:
                            break
                        total += len(chunk)
                        if total > _MAX_DRIVE_DOWNLOAD_BYTES:
                            raise ValidationError(
                                f"Drive download exceeded the 200 MiB cap for {filename!r}."
                            )
                        await await_with_deadline(
                            asyncio.to_thread(handle.write, chunk),
                            deadline,
                            on_timeout=TimeoutError,
                        )
                except BaseException as error:
                    exit_args = (type(error), error, error.__traceback__)
                    if response is not None:
                        abort = getattr(response, "abort", None)
                        if callable(abort):
                            abort()
                    raise
                finally:
                    if response_entered:
                        assert response_cm is not None
                        await _settle_context_exit(response_cm, *exit_args)
            finally:
                self._open_files.discard(handle)
        if total == 0:
            raise ValidationError("Drive returned 0 bytes; the file may be empty or inaccessible.")

    async def _download_drive_file(
        self,
        document_id: str,
    ) -> tuple[Path, str, str | None, Path]:
        """Finish credential-bearing Drive I/O before returning a temporary file."""

        ref = parse_drive_ref(document_id)
        deadline = RuntimeDeadline.start(self._upload_timeout, monotonic=self._monotonic)
        temp_dir: Path | None = None
        client: Any | None = None
        try:
            async with (
                self._transport.operation_scope("sources.add_drive_file.download") as lease,
                self._download_slot(),
            ):
                self._assert_epoch(lease.epoch)
                credential = await await_with_deadline(
                    self._bearer_provider.get(lease.epoch),
                    deadline,
                    on_timeout=TimeoutError,
                )
                self._assert_epoch(lease.epoch)
                client = self._client_factory()(
                    cookies=None,
                    follow_redirects=False,
                    timeout=self._http_timeout or deadline.remaining(),
                )
                self._transport_clients.add(client)
                async with client:
                    filename, content_type = await self._drive_metadata(
                        client,
                        ref,
                        credential,
                        deadline,
                    )
                    temp_dir = Path(tempfile.mkdtemp(prefix="nlm-android-drive-"))
                    _set_private_temp_permissions(temp_dir, 0o700)
                    path = temp_dir / filename
                    await self._stream_drive_media(
                        client,
                        ref,
                        filename,
                        credential,
                        path,
                        deadline,
                    )
            assert temp_dir is not None
            return path, filename, content_type, temp_dir
        except BaseException:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        finally:
            if client is not None:
                self._transport_clients.discard(client)

    def _drive_staging(self) -> DriveStagingTransfer:
        """Build the Drive staging collaborator over this pipeline's transport."""

        return DriveStagingTransfer(
            transport=self._transport,
            bearer_provider=self._bearer_provider,
            client_factory=self._client_factory,
            upload_slot=self._upload_slot,
            assert_epoch=self._assert_epoch,
            track_client=self._transport_clients.add,
            untrack_client=self._transport_clients.discard,
            upload_timeout=self._upload_timeout,
            http_timeout=self._http_timeout,
            monotonic=self._monotonic,
            bounded=partial(await_with_deadline, on_timeout=TimeoutError),
        )

    async def add_file_via_drive_staging(
        self,
        notebook_id: str,
        canonical_path: Path,
        mime_type: str | None,
        *,
        wait_timeout: float,
        title: str | None,
        import_drive_file: ImportDriveFile,
    ) -> Source:
        """Run Drive staging without retaining this bearer-owning pipeline."""

        pipeline = self
        result: Source | None = None
        failure: BaseException | None = None
        try:
            result = await pipeline._add_file_via_drive_staging_impl(
                notebook_id,
                canonical_path,
                mime_type,
                wait_timeout=wait_timeout,
                title=title,
                import_drive_file=import_drive_file,
            )
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            del self, pipeline
        if failure is not None:
            raise failure
        return cast(Source, result)

    async def _add_file_via_drive_staging_impl(
        self,
        notebook_id: str,
        canonical_path: Path,
        mime_type: str | None,
        *,
        wait_timeout: float,
        title: str | None,
        import_drive_file: ImportDriveFile,
    ) -> Source:
        """Add a file the mobile upload frontend cannot parse, by way of Drive.

        Three consequences the caller should know:

        * this path always waits for the source to be ready, whatever ``wait``
          asked for -- the staged copy cannot be removed until the import has
          materialized the content;
        * the resulting source is Drive-backed, so ``drive_status`` describes
          the staged copy that no longer exists rather than a live document;
        * ``on_progress`` is not reported -- staging is a single multipart
          request, not a chunked transfer.
        """
        content_type = _resolve_upload_content_type(canonical_path, mime_type)
        _validate_upload_file_supported(canonical_path, content_type)
        async with self._drive_staging().scope(
            canonical_path,
            canonical_path.name,
            content_type,
        ) as staged_file_id:
            return await import_drive_file(
                notebook_id,
                staged_file_id,
                title or canonical_path.name,
                mime_type=content_type,
                wait=True,
                wait_timeout=wait_timeout,
            )

    @asynccontextmanager
    async def drive_download_scope(
        self,
        document_id: str,
    ) -> AsyncIterator[tuple[Path, str, str | None]]:
        """Download one upload-only Drive file without exposing bearer-owned errors."""

        pipeline = self
        downloaded: tuple[Path, str, str | None, Path] | None = None
        failure: BaseException | None = None
        file_id = ""
        try:
            file_id = parse_drive_ref(document_id).file_id
            downloaded = await pipeline._download_drive_file(document_id)
        except BaseException as error:
            if isinstance(error, (httpx.HTTPError, OSError, TimeoutError)):
                failure = NetworkError(
                    f"Network error fetching Drive file {file_id} ({error.__class__.__name__})",
                    original_error=None,
                )
            else:
                failure = error
            sanitize_escaping_exception(error)
        finally:
            del document_id, file_id, self, pipeline

        if failure is not None:
            raise sanitize_escaping_exception(failure) from None
        assert downloaded is not None
        path, filename, content_type, temp_dir = downloaded
        try:
            yield path, filename, content_type
        finally:
            path.unlink(missing_ok=True)
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def upload_file(
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
        finalize_uploaded: _UploadedSourceFinalizer,
    ) -> Source:
        """Upload one NotebookLM-supported file without retaining secret owners."""

        pipeline = self
        result: Source | None = None
        failure: BaseException | None = None
        try:
            result = await pipeline._upload_file_impl(
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
                finalize_uploaded=finalize_uploaded,
            )
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            del self, pipeline
        if failure is not None:
            raise failure
        return cast(Source, result)

    async def _upload_file_impl(
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
        finalize_uploaded: _UploadedSourceFinalizer,
    ) -> Source:
        raw_path = Path(file_path)
        content_type = _resolve_upload_content_type(raw_path, mime_type)
        _validate_upload_file_supported(raw_path, content_type)
        _validate_project_id(notebook_id)
        deadline = RuntimeDeadline.start(self._upload_timeout, monotonic=self._monotonic)
        state = _UploadState()
        scope = self._transport.operation_scope("Android source upload")
        lease = await await_with_deadline(scope.__aenter__(), deadline, on_timeout=TimeoutError)
        try:
            try:
                source_id, filename = await await_with_deadline(
                    self._control_plane(
                        notebook_id,
                        raw_path,
                        deadline,
                        state,
                        on_progress,
                        content_type,
                        lease.epoch,
                        register_tentative,
                    ),
                    deadline,
                    on_timeout=TimeoutError,
                )
            except TimeoutError:
                raise _upload_failure(
                    filename=raw_path.name, state=state, detail="timed out"
                ) from None

            async def _wait_until_ready(
                target_notebook_id: str,
                target_source_id: str,
                timeout: float,
            ) -> Source:
                return await wait_until_ready(
                    target_notebook_id,
                    target_source_id,
                    timeout,
                    lease.epoch,
                )

            async def _wait_until_registered(
                target_notebook_id: str,
                target_source_id: str,
                timeout: float,
            ) -> Source:
                return await wait_until_registered(
                    target_notebook_id,
                    target_source_id,
                    timeout,
                    lease.epoch,
                )

            async def _rename_uploaded(
                target_notebook_id: str,
                target_source_id: str,
                requested_title: str,
            ) -> str | None:
                return await rename_uploaded(
                    target_notebook_id,
                    target_source_id,
                    requested_title,
                    lease.epoch,
                )

            return await finalize_uploaded(
                notebook_id,
                source_id,
                filename,
                wait=wait,
                wait_timeout=wait_timeout,
                title=title,
                wait_until_ready=_wait_until_ready,
                wait_until_registered=_wait_until_registered,
                rename_uploaded=_rename_uploaded,
            )
        finally:
            await scope.__aexit__(None, None, None)

    async def _control_plane(
        self,
        notebook_id: str,
        raw_path: Path,
        deadline: RuntimeDeadline,
        state: _UploadState,
        on_progress: ProgressCallback | None,
        content_type: str,
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

        resolved = await await_with_deadline(
            asyncio.to_thread(_resolve_and_check), deadline, on_timeout=TimeoutError
        )
        filename = resolved.name
        semaphore = self._upload_slot()
        queued_at = self._monotonic()
        await await_with_deadline(semaphore.acquire(), deadline, on_timeout=TimeoutError)
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

            file_obj, file_size = await await_with_deadline(
                asyncio.to_thread(_open_and_stat), deadline, on_timeout=TimeoutError
            )
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
                    content_type,
                    expected_epoch,
                    deadline,
                ),
                expected_epoch,
            )
            if isinstance(start, _HTTPFailure):
                raise _upload_failure(filename, state, "request failed")
            if start.status_code == 401:
                error = AuthError(f"Authentication failed uploading {filename!r} (HTTP 401)")
                error.source_id = state.source_id  # type: ignore[attr-defined]
                error.stage = state.stage  # type: ignore[attr-defined]
                raise error
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
            if final.status_code == 401:
                error = AuthError(f"Authentication failed uploading {filename!r} (HTTP 401)")
                error.source_id = state.source_id  # type: ignore[attr-defined]
                error.stage = state.stage  # type: ignore[attr-defined]
                raise error
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
            credential = await await_with_deadline(
                self._bearer_provider.get(expected_epoch),
                deadline,
                on_timeout=TimeoutError,
            )
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
        content_type: str,
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
                "X-Goog-AuthUser": "0",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Content-Length": str(file_size),
                "X-Goog-Upload-File-Name": filename,
                "X-Goog-Upload-Header-Content-Length": str(file_size),
                "X-Goog-Upload-Header-Content-Type": content_type,
                "X-Goog-Upload-Protocol": "resumable",
            }
            client = self._client_factory()(
                cookies=None,
                follow_redirects=False,
                timeout=self._http_timeout or deadline.remaining(),
            )
            self._assert_epoch(expected_epoch)
            self._transport_clients.add(client)
            async with client:
                response = await await_with_deadline(
                    client.post(
                        url,
                        headers=headers,
                        content=build_upload_start_body(notebook_id, source_id),
                        follow_redirects=False,
                    ),
                    deadline,
                    on_timeout=TimeoutError,
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
                "X-Goog-AuthUser": "0",
                "X-Goog-Upload-Command": "upload, finalize",
                "X-Goog-Upload-Offset": "0",
            }
            client = self._client_factory()(
                cookies=None,
                follow_redirects=False,
                timeout=self._http_timeout or deadline.remaining(),
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
                    response = await await_with_deadline(
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
                        on_timeout=TimeoutError,
                    )
                else:
                    response = await await_with_deadline(
                        client.put(
                            session_url,
                            headers=headers,
                            content=_stream(),
                            follow_redirects=False,
                        ),
                        deadline,
                        on_timeout=TimeoutError,
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
