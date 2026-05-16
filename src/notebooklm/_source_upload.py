"""Private source file upload pipeline."""

from __future__ import annotations

import asyncio
import json
import os
import re
import warnings
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import IO, Any, Protocol

import httpx

from ._callbacks import maybe_await_callback
from ._capabilities import (
    AuthRouteProvider,
    CookieJarProvider,
    TransportOperationProvider,
    UploadConcurrencyProvider,
)
from ._env import get_base_url
from .exceptions import (
    AuthError,
    NetworkError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from .rpc import RPCError, RPCMethod, get_upload_url
from .rpc.types import SourceStatus
from .types import Source, SourceAddError

_SOURCE_ID_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class SourceUploadRuntime(
    UploadConcurrencyProvider,
    TransportOperationProvider,
    Protocol,
):
    """Capabilities needed by public file-upload orchestration."""


class SourceUploadHttpRuntime(
    AuthRouteProvider,
    CookieJarProvider,
    Protocol,
):
    """Capabilities needed by Scotty upload HTTP calls."""


class RpcCaller(Protocol):
    """RPC callback shape used by upload registration."""

    async def __call__(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
    ) -> Any: ...


class RegisterFileSource(Protocol):
    """Late-bound facade hook for file-source registration."""

    async def __call__(self, notebook_id: str, filename: str) -> str: ...


class StartResumableUpload(Protocol):
    """Late-bound facade hook for upload-session start."""

    async def __call__(
        self,
        notebook_id: str,
        filename: str,
        file_size: int,
        source_id: str,
    ) -> str: ...


class UploadFileStreaming(Protocol):
    """Late-bound facade hook for streaming upload finalize."""

    async def __call__(
        self,
        upload_url: str,
        file_obj: IO[bytes] | Path,
        *,
        filename: str | None = None,
        on_progress: Callable[[int, int], object] | None = None,
        total_bytes: int | None = None,
    ) -> None: ...


class WaitForSource(Protocol):
    """Wait helper used after upload registration."""

    async def __call__(
        self,
        notebook_id: str,
        source_id: str,
        timeout: float = 120.0,
    ) -> Source: ...


class RenameSource(Protocol):
    """Source rename callback shape."""

    async def __call__(self, notebook_id: str, source_id: str, new_title: str) -> Source: ...


class ResolveUploadTimeout(Protocol):
    """Upload timeout resolution callback shape."""

    def __call__(self, default: httpx.Timeout) -> httpx.Timeout: ...


class AsyncClientFactory(Protocol):
    """Factory for creating an ``httpx.AsyncClient``-compatible instance."""

    def __call__(
        self,
        *,
        timeout: httpx.Timeout,
        cookies: httpx.Cookies,
    ) -> httpx.AsyncClient: ...


class CancelUploadSession(Protocol):
    """Late-bound facade hook for best-effort upload cancellation."""

    async def __call__(self, upload_url: str, base_url: str, auth_route: str) -> None: ...


_BACKGROUND_CANCEL_TASKS: set[asyncio.Task[None]] = set()


def _retain_background_cancel_task(task: asyncio.Task[None]) -> None:
    _BACKGROUND_CANCEL_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_CANCEL_TASKS.discard)


def _extract_register_file_source_id(result: Any, filename: str) -> str | None:
    """Locate the SOURCE_ID string in an ADD_SOURCE_FILE response.

    The historical shape was a strictly position-0 walk: ``[[[[id]]]]``. Issue
    #474 surfaced cases where that walk lands on ``None`` or on the echoed
    filename and silently fails. Walk the whole structure instead, prefer a
    UUID-shaped leaf, and fall back to any other id-shaped string that is
    plausibly not a status label.
    """
    uuid_match: str | None = None
    fallback: str | None = None
    # Depth guard for malformed/adversarial payloads. Google's real responses
    # are shallow, so 50 is generous without risking RecursionError.
    max_depth = 50

    def walk(node: Any, depth: int) -> None:
        nonlocal uuid_match, fallback
        if uuid_match is not None or depth > max_depth:
            return
        if isinstance(node, str):
            if len(node) > 1000:
                return
            candidate = node.strip()
            if not candidate or candidate == filename:
                return
            if _SOURCE_ID_UUID_PATTERN.match(candidate):
                uuid_match = candidate
                return
            if fallback is None and _looks_like_id_string(candidate):
                fallback = candidate
        elif isinstance(node, list):
            for child in node:
                if uuid_match is not None:
                    return
                walk(child, depth + 1)

    walk(result, 0)
    return uuid_match or fallback


def _looks_like_id_string(candidate: str) -> bool:
    """Heuristic for the non-UUID fallback in file-source id extraction."""
    if len(candidate) < 4:
        return False
    if any(c in candidate for c in " \t/"):
        return False
    return any(c.isdigit() or c in "-_" for c in candidate)


class SourceUploadPipeline:
    """Own file registration and resumable upload orchestration."""

    async def add_file(
        self,
        notebook_id: str,
        file_path: str | Path,
        mime_type: str | None = None,
        wait: bool = False,
        wait_timeout: float = 120.0,
        *,
        title: str | None = None,
        on_progress: Callable[[int, int], object] | None = None,
        deprecation_warning_stacklevel: int = 2,
        capabilities: SourceUploadRuntime,
        register_file_source: RegisterFileSource,
        start_resumable_upload: StartResumableUpload,
        upload_file_streaming: UploadFileStreaming,
        wait_until_ready: WaitForSource,
        wait_until_registered: WaitForSource,
        rename: RenameSource,
        logger: Any,
    ) -> Source:
        """Add a file source to a notebook using resumable upload."""
        logger.debug("Adding file source to notebook %s: %s", notebook_id, file_path)
        if mime_type is not None:
            warnings.warn(
                "mime_type parameter is unused and will be removed in v0.X.0; "
                "rely on filename extension instead",
                DeprecationWarning,
                stacklevel=deprecation_warning_stacklevel,
            )
        if title is not None:
            title = title.strip()
            if not title:
                raise ValidationError("Title cannot be empty or whitespace-only")

        file_path = Path(file_path).resolve()
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        if not file_path.is_file():
            raise ValidationError(f"Not a regular file: {file_path}")

        filename = file_path.name
        upload_sem = capabilities.get_upload_semaphore()
        upload_wait_start = monotonic()
        async with upload_sem:
            capabilities.record_upload_queue_wait(monotonic() - upload_wait_start)
            file_obj = open(file_path, "rb")  # noqa: SIM115
            handed_off = False
            operation_token = None
            try:
                file_size = os.fstat(file_obj.fileno()).st_size
                operation_token = await capabilities.begin_transport_post(
                    f"source upload {filename}"
                )
                source_id = await register_file_source(notebook_id, filename)
                upload_url = await start_resumable_upload(
                    notebook_id,
                    filename,
                    file_size,
                    source_id,
                )
                handed_off = True
                await upload_file_streaming(
                    upload_url,
                    file_obj,
                    filename=filename,
                    on_progress=on_progress,
                    total_bytes=file_size,
                )
            finally:
                try:
                    if operation_token is not None:
                        await capabilities.finish_transport_post(operation_token)
                finally:
                    if not handed_off:
                        file_obj.close()

        needs_title_rename = title is not None and title != filename
        if wait:
            source = await wait_until_ready(notebook_id, source_id, timeout=wait_timeout)
        elif needs_title_rename:
            source = await wait_until_registered(notebook_id, source_id, timeout=wait_timeout)
        else:
            source = Source(
                id=source_id,
                title=filename,
                status=SourceStatus.PROCESSING,
                _type_code=None,
            )

        if needs_title_rename:
            try:
                assert title is not None
                renamed = await rename(notebook_id, source_id, title)
                source = replace(source, title=renamed.title or title)
            except (RPCError, NetworkError):
                logger.warning(
                    "Source %s uploaded but rename to %r failed",
                    source_id,
                    title,
                    exc_info=True,
                )

        return source

    async def register_file_source(
        self,
        notebook_id: str,
        filename: str,
        *,
        rpc_call: RpcCaller,
    ) -> str:
        """Register a file source intent and get SOURCE_ID."""
        params = [
            [[filename]],
            notebook_id,
            [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]

        try:
            result = await rpc_call(
                RPCMethod.ADD_SOURCE_FILE,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=False,
            )
        except (AuthError, RateLimitError, ServerError):
            raise
        except RPCError as exc:
            raise SourceAddError(
                filename,
                cause=exc,
                message=f"Failed to register file source for {filename}: {exc}",
            ) from exc

        source_id = _extract_register_file_source_id(result, filename)
        if source_id:
            return source_id

        if isinstance(result, str):
            preview = repr(result[:200])
            if len(result) > 200:
                preview += "..."
        else:
            preview = repr(result)
            if len(preview) > 200:
                preview = preview[:200] + "..."
        raise SourceAddError(
            filename,
            message=(
                f"Failed to get SOURCE_ID from registration response. Response shape: {preview}"
            ),
        )

    async def start_resumable_upload(
        self,
        notebook_id: str,
        filename: str,
        file_size: int,
        source_id: str,
        *,
        capabilities: SourceUploadHttpRuntime,
        resolve_upload_timeout: ResolveUploadTimeout,
        async_client_factory: AsyncClientFactory,
    ) -> str:
        """Start a resumable upload session and get the upload URL."""
        auth_route = capabilities.authuser_header()
        base_url = get_base_url()
        url = f"{get_upload_url()}?{capabilities.authuser_query()}"

        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": base_url,
            "Referer": f"{base_url}/",
            "x-goog-authuser": auth_route,
            "x-goog-upload-command": "start",
            "x-goog-upload-header-content-length": str(file_size),
            "x-goog-upload-protocol": "resumable",
        }

        body = json.dumps(
            {
                "PROJECT_ID": notebook_id,
                "SOURCE_NAME": filename,
                "SOURCE_ID": source_id,
            }
        )

        async with async_client_factory(
            timeout=resolve_upload_timeout(httpx.Timeout(10.0, read=60.0)),
            cookies=capabilities.live_cookies(),
        ) as client:
            response = await client.post(url, headers=headers, content=body)
            response.raise_for_status()

            upload_url = response.headers.get("x-goog-upload-url")
            if not upload_url:
                raise SourceAddError(
                    filename, message="Failed to get upload URL from response headers"
                )

            return upload_url

    async def upload_file_streaming(
        self,
        upload_url: str,
        file_obj: IO[bytes] | Path,
        *,
        filename: str | None = None,
        on_progress: Callable[[int, int], object] | None = None,
        total_bytes: int | None = None,
        capabilities: SourceUploadHttpRuntime,
        resolve_upload_timeout: ResolveUploadTimeout,
        async_client_factory: AsyncClientFactory,
        cancel_upload_session: CancelUploadSession,
        logger: Any,
    ) -> None:
        """Stream upload file content to the resumable upload URL."""
        path_fallback: Path | None = file_obj if isinstance(file_obj, Path) else None
        close_wired = False
        try:
            base_url = get_base_url()
            auth_route = capabilities.authuser_header()
            headers = {
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                "x-goog-authuser": auth_route,
                "Origin": base_url,
                "Referer": f"{base_url}/",
                "x-goog-upload-command": "upload, finalize",
                "x-goog-upload-offset": "0",
            }
            diag_name = filename or (path_fallback.name if path_fallback is not None else "<file>")
            logger.debug("Streaming upload to %s for %s", upload_url, diag_name)
            if total_bytes is None and path_fallback is not None:
                total_bytes = path_fallback.stat().st_size
            progress_total = total_bytes if total_bytes is not None else 0
            uploaded_bytes = 0

            if on_progress is not None:
                await maybe_await_callback(on_progress, uploaded_bytes, progress_total)

            async def file_stream():
                nonlocal uploaded_bytes
                if path_fallback is not None:
                    with open(path_fallback, "rb") as f:
                        while chunk := await asyncio.to_thread(f.read, 65536):
                            uploaded_bytes += len(chunk)
                            if on_progress is not None:
                                await maybe_await_callback(
                                    on_progress, uploaded_bytes, progress_total
                                )
                            yield chunk
                    return

                assert not isinstance(file_obj, Path)
                while chunk := await asyncio.to_thread(file_obj.read, 65536):
                    uploaded_bytes += len(chunk)
                    if on_progress is not None:
                        await maybe_await_callback(on_progress, uploaded_bytes, progress_total)
                    yield chunk

            finalize_started = False

            async def _do_finalize() -> None:
                nonlocal finalize_started
                async with async_client_factory(
                    timeout=resolve_upload_timeout(httpx.Timeout(10.0, read=300.0)),
                    cookies=capabilities.live_cookies(),
                ) as client:
                    finalize_started = True
                    response = await client.post(upload_url, headers=headers, content=file_stream())
                    response.raise_for_status()

            def _on_finalize_done(t: asyncio.Task[None]) -> None:
                if path_fallback is None:
                    try:
                        file_obj.close()  # type: ignore[union-attr]
                    except Exception as close_exc:  # noqa: BLE001
                        logger.debug("Caller FD close in finalize-done failed: %r", close_exc)
                if not t.cancelled() and (exc := t.exception()) is not None:
                    logger.debug("Background finalize POST failed: %r", exc)

            finalize_task = asyncio.create_task(_do_finalize())
            finalize_task.add_done_callback(_on_finalize_done)
            close_wired = True
            try:
                await asyncio.shield(finalize_task)
            except asyncio.CancelledError:
                if not finalize_started:
                    finalize_task.cancel()
                    _retain_background_cancel_task(
                        asyncio.create_task(cancel_upload_session(upload_url, base_url, auth_route))
                    )
                    raise
                try:
                    await asyncio.shield(finalize_task)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Background finalize POST failed before cancellation propagated: %r",
                        exc,
                    )
                raise
        except BaseException:
            if not close_wired and path_fallback is None:
                try:
                    file_obj.close()  # type: ignore[union-attr]
                except Exception as close_exc:  # noqa: BLE001
                    logger.debug("Caller FD close on pre-wire exception failed: %r", close_exc)
            raise

    async def cancel_upload_session(
        self,
        upload_url: str,
        base_url: str,
        auth_route: str,
        *,
        capabilities: SourceUploadHttpRuntime,
        async_client_factory: AsyncClientFactory,
        logger: Any,
    ) -> None:
        """Best-effort POST a Scotty resumable-upload cancel command."""
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "x-goog-authuser": auth_route,
            "Origin": base_url,
            "Referer": f"{base_url}/",
            "x-goog-upload-command": "cancel",
        }
        try:
            async with async_client_factory(
                timeout=httpx.Timeout(10.0, read=10.0),
                cookies=capabilities.live_cookies(),
            ) as client:
                await client.post(upload_url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Best-effort Scotty cancel for %s failed: %r", upload_url, exc)


__all__ = [
    "SourceUploadPipeline",
    "_SOURCE_ID_UUID_PATTERN",
    "_extract_register_file_source_id",
    "_looks_like_id_string",
]
