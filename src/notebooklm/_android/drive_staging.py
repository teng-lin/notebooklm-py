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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from .._deadline import RuntimeDeadline
from ..exceptions import (
    AuthError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from ..types import Source

logger = logging.getLogger(__name__)

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
        size = await asyncio.to_thread(lambda: file_path.stat().st_size)
        if size > _MAX_DRIVE_STAGING_BYTES:
            raise ValidationError(
                f"{filename} is {size} bytes; Drive staging is capped at "
                f"{_MAX_DRIVE_STAGING_BYTES} bytes."
            )
        payload = await asyncio.to_thread(file_path.read_bytes)
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
                    await self._bounded(
                        client.delete(
                            f"{DRIVE_API_ORIGIN}/drive/v3/files/"
                            f"{quote(file_id, safe='')}?supportsAllDrives=true",
                            headers={"Authorization": f"Bearer {credential.token}"},
                            follow_redirects=False,
                        ),
                        deadline,
                    )
        except Exception:
            # An orphaned staging file is untidy, not incorrect. Surfacing this
            # would replace a successful add -- or the real failure -- with a
            # cleanup error the caller cannot act on.
            logger.warning(
                "Could not delete the staged Drive file %s; remove it manually if it persists.",
                file_id,
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
            failure = sanitize_escaping_exception(error)
        finally:
            del self

        if failure is not None:
            raise failure
        assert file_id is not None
        try:
            yield file_id
        finally:
            await transfer.unstage(file_id)


__all__ = [
    "DRIVE_API_ORIGIN",
    "DriveStagingTransfer",
    "ImportDriveFile",
    "build_multipart_body",
    "map_staging_status",
]
