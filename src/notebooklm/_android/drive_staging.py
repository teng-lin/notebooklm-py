"""Drive round-trip for files the mobile upload frontend will not parse.

The mobile Scotty frontend parses a narrow content-type set -- the same one the
app's own picker offers -- while the mobile *backend* parses everything Drive
hands it. Files outside the upload allowlist therefore reach the backend by way
of the caller's own Drive, using the ``auth/drive`` scope the Android identity
already holds. Full evidence: docs/android/web-compat-seam-closure.md.

This lives beside :mod:`notebooklm._android.upload` rather than inside it: the
Scotty transaction and the Drive transfer are separate protocols that happen to
serve the same public ``add_file``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from .._deadline import RuntimeDeadline
from ..exceptions import (
    AuthError,
    NetworkError,
    RateLimitError,
    ServerError,
    SourceTimeoutError,
    ValidationError,
)
from ..types import Source

logger = logging.getLogger(__name__)

#: Extensions the mobile upload frontend will not parse, which therefore reach
#: the backend by way of Drive instead (``add_file_via_drive_staging``).
#:
#: The mobile frontend's allowlist
#: mirrors the app's own picker -- audio and PDF -- plus plain text and
#: markdown; the mobile *backend* parses everything Drive hands it. Both
#: members were live-probed on the raw native transaction (Web READY, Android
#: SOURCE_STATUS_ERROR) and then through the Drive route (READY, correct text).
#: Full evidence: docs/android/web-compat-seam-closure.md.
#:
#: Sending them over the native transaction cannot be made to work: every
#: plausible declared content type -- the OOXML types, ``application/zip``,
#: ``application/epub+zip`` (a ZIP type the frontend demonstrably accepts for
#: ``.epub``), ``application/vnd.google-apps.document`` -- errors, and the one
#: type that does not, ``text/plain``, reports READY while ingesting the raw
#: ZIP container as text (``PK\x03\x04...``, none of the document's words
#: present). The Scotty ingester dispatches on the declared type and has no
#: OOXML parser behind it; the Drive import reaches a different backend entry
#: point that does.
#:
#: This set is one arm of an exhaustive partition of
#: :data:`~notebooklm._types.sources._UPLOAD_FILE_EXTENSIONS`; see
#: :data:`_NATIVE_UPLOAD_EXTENSIONS` below. ``tests/unit/android`` fails if a
#: newly supported extension is not classified into exactly one arm, so a new
#: file type cannot silently inherit the wrong upload path.
_DRIVE_STAGED_UPLOAD_EXTENSIONS = frozenset({".csv", ".docx", ".pptx"})

#: Extensions the mobile Scotty frontend ingests directly.
#:
#: ``.epub`` shows the frontend's allowlist is not the app's picker set: the app
#: uploads only ``{pdfFile, imageFile, audioFile}`` (``UserFileUploadContentType``),
#: yet ``application/epub+zip``, ``text/plain`` and ``text/markdown`` all ingest
#: natively. The allowlist can only be established by probing, never read off
#: the APK.
_NATIVE_UPLOAD_EXTENSIONS = frozenset({".epub", ".markdown", ".md", ".pdf", ".txt"})


DRIVE_API_ORIGIN = "https://www.googleapis.com"
# Staging reads the whole file into memory to build one multipart body, so the
# cap is deliberately lower than the streaming Drive *download* cap.
_MAX_DRIVE_STAGING_BYTES = 50 * 1024 * 1024
_BOUNDARY = "notebooklm-android-staging"
_UPLOAD_URL = f"{DRIVE_API_ORIGIN}/upload/drive/v3/files?uploadType=multipart"


class ImportDriveFile(Protocol):
    """The adapter's Drive import, used to land a staged file as a source."""

    async def __call__(
        self,
        notebook_id: str,
        file_id: str,
        title: str,
        mime_type: str = ...,
        *,
        wait: bool = ...,
        wait_timeout: float = ...,
    ) -> Source: ...


def build_multipart_body(payload: bytes, filename: str, content_type: str) -> bytes:
    """Build the ``multipart/related`` body Drive's v3 upload endpoint expects."""

    metadata = json.dumps({"name": filename, "mimeType": content_type}, ensure_ascii=False)
    return b"".join(
        (
            f"--{_BOUNDARY}\r\n".encode(),
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
            metadata.encode("utf-8"),
            f"\r\n--{_BOUNDARY}\r\n".encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            payload,
            f"\r\n--{_BOUNDARY}--".encode(),
        )
    )


def map_staging_status(status: int, filename: str) -> None:
    """Translate a Drive HTTP status into the public exception taxonomy."""

    if status == 401:
        raise AuthError(
            "Android Drive authentication expired; reauthenticate the selected profile."
        )
    if status == 429:
        raise RateLimitError(
            f"Drive throttled the staging upload for {filename}; retry after a delay."
        )
    if status == 403:
        raise ValidationError(
            f"Drive refused the staging upload for {filename}. The selected account needs "
            "Drive access and free quota for this file type."
        )
    if status >= 500:
        raise ServerError(
            f"Drive returned HTTP {status} while staging {filename}; retry later.",
            status_code=status,
        )
    if status >= 300:
        raise ValidationError(f"Drive returned HTTP {status} while staging {filename}.")


class _StagingCleanupFailed(Exception):
    """Drive refused the cleanup DELETE. Never escapes ``unstage``."""

    def __init__(self, status: int) -> None:
        super().__init__(f"Drive returned HTTP {status} deleting the staged file")
        self.status = status


class DriveStagingTransfer:
    """Stage a local file in Drive and remove it again.

    Constructed by :class:`~notebooklm._android.upload.AndroidUploadPipeline`
    with the pipeline's own transport collaborators, so staging obeys the same
    epoch, deadline, and client-tracking discipline as every other Android
    transfer.
    """

    def __init__(
        self,
        *,
        transport: Any,
        bearer_provider: Any,
        client_factory: Callable[[], Any],
        upload_slot: Callable[[], Any],
        assert_epoch: Callable[[int], None],
        track_client: Callable[[Any], None],
        untrack_client: Callable[[Any], None],
        upload_timeout: float,
        http_timeout: Any,
        monotonic: Callable[[], float],
        bounded: Callable[[Any, RuntimeDeadline], Awaitable[Any]],
    ) -> None:
        self._transport = transport
        self._bearer_provider = bearer_provider
        self._client_factory = client_factory
        self._upload_slot = upload_slot
        self._assert_epoch = assert_epoch
        self._track_client = track_client
        self._untrack_client = untrack_client
        self._upload_timeout = upload_timeout
        self._http_timeout = http_timeout
        self._monotonic = monotonic
        self._bounded = bounded

    def _deadline(self) -> RuntimeDeadline:
        return RuntimeDeadline.start(self._upload_timeout, monotonic=self._monotonic)

    async def stage(self, file_path: Path, filename: str, content_type: str) -> str:
        """Upload one local file to the caller's Drive; return the new file id."""

        deadline = self._deadline()

        def _read_regular_file_bounded() -> bytes:
            """Validate and read through ONE descriptor.

            Checking the path and then re-resolving it by name leaves a window
            in which the file can be swapped for a FIFO -- whose ``open`` blocks
            a worker thread no deadline bounds and cancellation cannot stop --
            or grown past the cap before the whole of it is allocated. Opening
            once with ``O_NONBLOCK`` (a no-op for regular files, and what keeps
            the FIFO open from hanging), then ``fstat``-ing that descriptor,
            closes both.
            """
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(file_path, flags)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValidationError(f"Not a regular file: {file_path}")
                if info.st_size > _MAX_DRIVE_STAGING_BYTES:
                    raise ValidationError(
                        f"{filename} is {info.st_size} bytes; Drive staging is capped "
                        f"at {_MAX_DRIVE_STAGING_BYTES} bytes."
                    )
                handle = os.fdopen(descriptor, "rb", closefd=True)
            except BaseException:
                os.close(descriptor)
                raise
            # One byte past the cap so a file that grew after the fstat is
            # detected without allocating all of it.
            with handle:
                content = handle.read(_MAX_DRIVE_STAGING_BYTES + 1)
            if len(content) > _MAX_DRIVE_STAGING_BYTES:
                read_bytes = len(content)
                del content
                raise ValidationError(
                    f"{filename} is at least {read_bytes} bytes; Drive staging is "
                    f"capped at {_MAX_DRIVE_STAGING_BYTES} bytes."
                )
            return content

        payload = await asyncio.to_thread(_read_regular_file_bounded)
        body = build_multipart_body(payload, filename, content_type)
        del payload

        client: Any | None = None
        try:
            async with (
                self._transport.operation_scope("sources.add_file.drive_stage") as lease,
                self._upload_slot(),
            ):
                self._assert_epoch(lease.epoch)
                credential = await self._bounded(self._bearer_provider.get(lease.epoch), deadline)
                self._assert_epoch(lease.epoch)
                client = self._client_factory()(
                    cookies=None,
                    follow_redirects=False,
                    timeout=self._http_timeout or deadline.remaining(),
                )
                self._track_client(client)
                async with client:
                    response = await self._bounded(
                        client.post(
                            _UPLOAD_URL,
                            headers={
                                "Authorization": f"Bearer {credential.token}",
                                "Content-Type": (f"multipart/related; boundary={_BOUNDARY}"),
                            },
                            content=body,
                            follow_redirects=False,
                        ),
                        deadline,
                    )
                    status = int(response.status_code)
                    if status == 401:
                        self._bearer_provider.invalidate(credential.generation)
                    map_staging_status(status, filename)
                    try:
                        created = response.json()
                    except (TypeError, ValueError):
                        raise ValidationError(
                            f"Drive returned malformed metadata while staging {filename}."
                        ) from None
                    file_id = created.get("id") if isinstance(created, dict) else None
                    if not isinstance(file_id, str) or not file_id:
                        raise ValidationError(
                            f"Drive did not return a file id while staging {filename}."
                        )
                    return file_id
        finally:
            if client is not None:
                self._untrack_client(client)

    async def unstage(self, file_id: str) -> None:
        """Delete a staged Drive file. Best effort: never masks the real outcome."""

        deadline = self._deadline()
        client: Any | None = None
        try:
            async with self._transport.operation_scope("sources.add_file.drive_unstage") as lease:
                credential = await self._bounded(self._bearer_provider.get(lease.epoch), deadline)
                client = self._client_factory()(
                    cookies=None,
                    follow_redirects=False,
                    timeout=self._http_timeout or deadline.remaining(),
                )
                self._track_client(client)
                async with client:
                    response = await self._bounded(
                        client.delete(
                            f"{DRIVE_API_ORIGIN}/drive/v3/files/"
                            f"{quote(file_id, safe='')}?supportsAllDrives=true",
                            headers={"Authorization": f"Bearer {credential.token}"},
                            follow_redirects=False,
                        ),
                        deadline,
                    )
                    status = int(response.status_code)
                    # 404 means it is already gone, which is the desired end
                    # state. Anything else non-2xx left the file in place.
                    if status >= 300 and status != 404:
                        raise _StagingCleanupFailed(status)
        except Exception as error:
            # An orphaned staging file is untidy, not incorrect. Surfacing this
            # would replace a successful add -- or the real failure -- with a
            # cleanup error the caller cannot act on. It is logged at WARNING
            # with the id so it can be found and removed.
            detail = (
                f"HTTP {error.status}"
                if isinstance(error, _StagingCleanupFailed)
                else type(error).__name__
            )
            logger.warning(
                "Could not delete the staged Drive file %s (%s); "
                "remove it manually if it persists.",
                file_id,
                detail,
            )
        finally:
            if client is not None:
                self._untrack_client(client)

    @asynccontextmanager
    async def scope(
        self,
        file_path: Path,
        filename: str,
        content_type: str,
    ) -> AsyncIterator[str]:
        """Stage the file for the duration of the block, then remove it.

        Deleting the staged copy is safe once the source is ready: the import
        materializes the content, and a live probe confirmed the source stays
        ``READY`` with its text intact afterwards.
        """
        from .errors import sanitize_escaping_exception

        transfer = self
        file_id: str | None = None
        failure: BaseException | None = None
        try:
            file_id = await transfer.stage(file_path, filename, content_type)
        except BaseException as error:
            # Match ``drive_download_scope``: a transport-level failure reaches
            # callers as NetworkError, so retry-by-public-exception-type works
            # the same on this path as on every other Android transfer.
            if isinstance(error, (httpx.HTTPError, OSError, TimeoutError)):
                failure = NetworkError(
                    f"Network error staging {filename} in Drive ({error.__class__.__name__})",
                    original_error=None,
                )
            else:
                failure = error
            sanitize_escaping_exception(error)
        finally:
            del self

        if failure is not None:
            raise sanitize_escaping_exception(failure) from None
        assert file_id is not None

        try:
            yield file_id
        except (asyncio.CancelledError, TimeoutError, SourceTimeoutError):
            # Ambiguous: the import may still be running server-side. Deleting
            # the only copy now can turn a slow-but-successful import into a
            # permanently errored source, so the file is kept and named instead.
            # A leaked file is recoverable; a broken source is not.
            logger.warning(
                "Left the staged Drive file %s in place: the import did not settle "
                "and may still be reading it. Remove it once the source is READY "
                "or has failed.",
                file_id,
            )
            raise
        except BaseException:
            # A settled failure -- the import will not complete, so the staged
            # copy is dead weight.
            await transfer.unstage(file_id)
            raise
        else:
            await transfer.unstage(file_id)


__all__ = [
    "DRIVE_API_ORIGIN",
    "_DRIVE_STAGED_UPLOAD_EXTENSIONS",
    "_NATIVE_UPLOAD_EXTENSIONS",
    "DriveStagingTransfer",
    "ImportDriveFile",
    "build_multipart_body",
    "map_staging_status",
]
