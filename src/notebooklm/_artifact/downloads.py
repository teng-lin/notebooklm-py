"""Backend-neutral artifact asset transfer helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .._auth.cookies import load_httpx_cookies
from .._curl_cffi_transport import resolve_transport_factory
from .._hop_credentials import CredentialPolicy, HopCredentials
from ..exceptions import ArtifactDownloadError
from ._download_client import (
    _download_display_host,
    _is_trusted_download_host,
    _make_download_client,
)
from ._redirect_guard import redirect_revalidation_hooks

logger = logging.getLogger(__name__)
_DOWNLOAD_WRITER_QUEUE_SIZE = 8


def _credential_policy(cookies: Any) -> CredentialPolicy:
    """Return the web default: the jar on trusted hops, otherwise no credential."""
    cookie_jar = cookies if isinstance(cookies, httpx.Cookies) else httpx.Cookies(cookies)

    def credential_for(url: str) -> HopCredentials | None:
        parsed = urlparse(url)
        if parsed.scheme == "https" and _is_trusted_download_host(parsed.hostname):
            return HopCredentials(cookies=cookie_jar)
        return None

    return credential_for


async def _await_writer_exit(
    writer_thread: threading.Thread,
    *,
    re_raise_cancel: bool = False,
) -> None:
    """Wait for a download writer thread to actually exit.

    A plain ``await asyncio.to_thread(thread.join)`` is unsafe under cancellation:
    the await raises ``CancelledError`` and we unwind while the underlying join is
    still blocked, so outer cleanup (``temp_file.unlink``) races the writer's
    still-open file handle. ``asyncio.shield`` alone doesn't help (the await still
    raises). The fix is a shield-loop that re-awaits the same shielded join task
    until it completes; repeated cancellations only delay our re-raise, never the
    writer's exit.

    Only ``CancelledError`` is caught (any other join exception propagates). The
    most recent ``CancelledError`` is preserved and, when ``re_raise_cancel`` is
    set, re-raised after the writer exits — success-path callers want this so an
    in-flight cancellation isn't lost; cleanup-path callers leave it ``False`` so
    the original error isn't masked by a second cancellation.
    """
    join_task = asyncio.ensure_future(asyncio.to_thread(writer_thread.join))
    cancelled_error: asyncio.CancelledError | None = None
    while not join_task.done():
        try:
            await asyncio.shield(join_task)
        except asyncio.CancelledError as exc:
            # Outer task was cancelled. The shielded join keeps
            # running; loop and re-await so the writer can still
            # exit cleanly before we return.
            cancelled_error = exc

    if cancelled_error is not None and re_raise_cancel:
        raise cancelled_error


@dataclass(frozen=False)
class DownloadResult:
    """Outcome of a multi-URL download batch.

    Replaces the v0 silent-partial-failure behavior where `_download_urls_batch`
    returned only successful paths. Callers can now distinguish "all succeeded"
    from "partial" via the properties below.

    `succeeded`: paths that downloaded cleanly (matches existing list[str] shape).
    `failed`: (url, exception) tuples for transport, URL parsing, or download failures.
    """

    succeeded: list[str] = field(default_factory=list)
    failed: list[tuple[str, Exception]] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        return not self.failed

    @property
    def partial(self) -> bool:
        return bool(self.succeeded) and bool(self.failed)


def _load_httpx_cookies(storage_path: Any) -> Any:
    return load_httpx_cookies(path=storage_path)


def _reject_html_download(response: httpx.Response) -> None:
    """Reject an HTML body served where a media file was expected (usually expired auth).

    Shared by both ``download_url`` transport branches (curl_cffi buffered + httpx
    streaming), which detect this the same way and raise the same guidance.
    """
    if "text/html" in response.headers.get("content-type", ""):
        raise ArtifactDownloadError(
            "media",
            details="Download failed: received HTML instead of media file. "
            "Authentication may have expired. Run 'notebooklm login'.",
        )


def _reject_empty_download(total_bytes: int) -> None:
    """Reject a zero-byte download (the remote file is missing or empty)."""
    if total_bytes == 0:
        raise ArtifactDownloadError(
            "media",
            details="Download produced 0 bytes -- the remote file may be missing or empty",
        )


class AssetDownloadService:
    """Shared streaming, rejection, staging, and atomic-replace asset plane."""

    def __init__(
        self,
        *,
        storage_path: Path | None = None,
        cookie_loader: Callable[[Any], Any] = _load_httpx_cookies,
        credential_policy_factory: Callable[[Any], CredentialPolicy] = _credential_policy,
    ) -> None:
        self._storage_path = storage_path
        self._cookie_loader = cookie_loader
        self._credential_policy_factory = credential_policy_factory

    async def download_urls_batch(self, urls_and_paths: list[tuple[str, str]]) -> DownloadResult:
        """Download multiple files using httpx with proper cookie handling."""
        result = DownloadResult()

        cookies = await asyncio.to_thread(self._cookie_loader, self._storage_path)
        credential_for = self._credential_policy_factory(cookies)

        client, _guarded_get = _make_download_client(
            cookies, timeout=60.0, credential_for=credential_for
        )
        async with client:
            for url, output_path in urls_and_paths:
                display_host = ""
                parsed_path = ""
                try:
                    parsed = urlparse(url)
                    display_host = _download_display_host(parsed)
                    parsed_path = parsed.path
                    if parsed.scheme != "https":
                        raise ArtifactDownloadError(
                            "media", details=f"Download URL must use HTTPS: {url[:80]}"
                        )
                    if not _is_trusted_download_host(parsed.hostname):
                        raise ArtifactDownloadError(
                            "media",
                            details=f"Untrusted download domain: {display_host}",
                        )

                    response = await _guarded_get(url)
                    if response.status_code in (401, 403):
                        raise ArtifactDownloadError(
                            "media",
                            details=(
                                f"Authentication failed (HTTP {response.status_code}) "
                                f"on {display_host}{parsed.path}"
                            ),
                        )
                    response.raise_for_status()

                    content_type = response.headers.get("content-type", "")
                    if "text/html" in content_type:
                        raise ArtifactDownloadError(
                            "media", details="Received HTML instead of media file"
                        )

                    output_file = Path(output_path)
                    output_file.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(output_file.write_bytes, response.content)
                    result.succeeded.append(output_path)
                    logger.debug(
                        "Downloaded %s%s (%d bytes)",
                        display_host,
                        parsed.path,
                        len(response.content),
                    )

                except (httpx.HTTPError, ValueError, ArtifactDownloadError) as e:
                    # ``ArtifactDownloadError`` covers the policy violations
                    # raised earlier in this block (non-HTTPS scheme,
                    # untrusted host, 401/403, HTML payload). Aggregating
                    # them into ``result.failed`` lets a single bad URL
                    # fall out of the batch instead of aborting every
                    # remaining download in the loop. The single-URL
                    # ``download_url`` path below intentionally still
                    # raises — only the batch surface absorbs.
                    if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                        reason = f"HTTP {e.response.status_code}"
                    else:
                        reason = e.__class__.__name__
                    logger.warning(
                        "Download failed for %s%s: %s",
                        display_host,
                        parsed_path,
                        reason,
                    )
                    result.failed.append((url, e))

        return result

    async def download_url(self, url: str, output_path: str) -> str:
        """Download a file from URL using streaming with proper cookie handling."""
        parsed = urlparse(url)
        display_host = _download_display_host(parsed)
        if parsed.scheme != "https":
            raise ArtifactDownloadError("media", details=f"Download URL must use HTTPS: {url[:80]}")
        if not _is_trusted_download_host(parsed.hostname):
            raise ArtifactDownloadError(
                "media",
                details=f"Untrusted download domain: {display_host}",
            )

        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        fd, temp_path_str = tempfile.mkstemp(
            dir=output_file.parent,
            prefix=output_file.name + ".",
            suffix=".tmp",
        )
        os.close(fd)
        temp_file = Path(temp_path_str)

        try:
            cookies = await asyncio.to_thread(self._cookie_loader, self._storage_path)
            credential_for = self._credential_policy_factory(cookies)
            timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0)

            try:
                # Transport selection is inlined here (rather than via
                # _make_download_client) because the httpx path below streams to
                # disk via the producer/consumer writer queue; _make_download_client
                # returns a buffering GET suited to download_urls_batch.
                factory = resolve_transport_factory()
                if factory is not httpx.AsyncClient:
                    # curl_cffi opt-in: libcurl's internal redirect loop can't host
                    # the #1521 per-hop event hook, so use the manual guarded GET
                    # (same trusted-host allowlist, re-checked per hop). It buffers
                    # rather than streams — acceptable for the opt-in transport,
                    # which already buffers RPC and upload bodies.
                    async with factory(
                        cookies=None, follow_redirects=False, timeout=timeout
                    ) as client:
                        response = await client.get_guarded(
                            url,
                            is_trusted_host=_is_trusted_download_host,
                            credential_for=credential_for,
                        )
                        response.raise_for_status()
                        _reject_html_download(response)
                        _reject_empty_download(len(response.content))
                        await asyncio.to_thread(temp_file.write_bytes, response.content)
                    os.replace(temp_file, output_file)
                    logger.debug(
                        "Downloaded %s%s (%d bytes)",
                        display_host,
                        parsed.path,
                        len(response.content),
                    )
                    return output_path
                async with httpx.AsyncClient(  # noqa: SIM117
                    cookies=cookies,
                    follow_redirects=True,
                    timeout=timeout,
                    event_hooks=redirect_revalidation_hooks(
                        _is_trusted_download_host, credential_for
                    ),  # #1521 + per-hop credentials
                ) as client:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        _reject_html_download(response)

                        # Producer/consumer split: one dedicated ``threading.Thread``
                        # (not ``asyncio.to_thread``, which would tie up a default-
                        # executor slot and risk deadlocking producers under many
                        # concurrent downloads) drains a bounded queue to
                        # ``temp_file``, avoiding per-chunk thread-pool churn on
                        # multi-GB files. Producer puts use ``put_nowait`` first,
                        # falling back to ``to_thread(put)`` only when full. EOF is a
                        # ``None`` sentinel; writer failures surface via
                        # ``writer_error`` + an early ``writer_failed`` Event so the
                        # producer can short-circuit before the drain completes.
                        chunk_q: queue.Queue[bytes | None] = queue.Queue(
                            maxsize=_DOWNLOAD_WRITER_QUEUE_SIZE
                        )
                        writer_failed = threading.Event()
                        writer_error: list[BaseException] = []

                        def _writer_loop() -> None:
                            # On writer failure the bounded queue may have a producer
                            # parked in ``q.put``; the ``finally`` drains via
                            # ``get_nowait`` so those puts complete and the producer
                            # can observe the failure. ``writer_failed`` is set in
                            # ``except`` BEFORE the drain so the producer short-
                            # circuits as early as possible.
                            try:
                                with open(temp_file, "wb") as fh:
                                    while True:
                                        item = chunk_q.get()
                                        if item is None:
                                            return
                                        fh.write(item)
                            except BaseException as exc:
                                # Capture-and-don't-reraise: the producer
                                # surfaces the exception via
                                # ``writer_error[0]`` after joining.
                                # Re-raising here would only land in the
                                # thread's bootstrap as
                                # ``PytestUnhandledThreadExceptionWarning``
                                # / sys.unraisablehook noise without
                                # carrying any new information.
                                writer_error.append(exc)
                                writer_failed.set()
                            finally:
                                while True:
                                    try:
                                        chunk_q.get_nowait()
                                    except queue.Empty:
                                        break

                        writer_thread = threading.Thread(
                            target=_writer_loop,
                            name=f"artifact-dl-writer-{temp_file.name}",
                            daemon=True,
                        )
                        writer_thread.start()
                        total_bytes = 0
                        try:
                            async for chunk in response.aiter_bytes(chunk_size=65536):
                                if writer_failed.is_set():
                                    # Writer raised mid-stream: stop reading (further
                                    # bytes would just be drained); error re-raised
                                    # via ``writer_error`` below.
                                    break
                                # ``put_nowait`` avoids a ``to_thread`` round-trip
                                # when the queue has space; fall back only when full
                                # so the loop suspends cleanly under back-pressure.
                                try:
                                    chunk_q.put_nowait(chunk)
                                except queue.Full:
                                    await asyncio.to_thread(chunk_q.put, chunk)
                                total_bytes += len(chunk)
                            if not writer_failed.is_set():
                                try:
                                    chunk_q.put_nowait(None)
                                except queue.Full:
                                    await asyncio.to_thread(chunk_q.put, None)
                            # ``_await_writer_exit`` shield-loops until the writer
                            # exits (so cleanup never races its file handle) and
                            # surfaces any captured exception; ``re_raise_cancel``
                            # preserves a cancellation that arrived mid-wait.
                            await _await_writer_exit(writer_thread, re_raise_cancel=True)
                            if writer_error:
                                raise next(iter(writer_error))  # one-slot exception box
                        except BaseException:
                            # On producer-side failure, ensure the writer sees a
                            # sentinel and exits even if the queue is saturated: a
                            # bare ``put_nowait(None)`` would raise ``queue.Full`` and
                            # leave the writer parked forever, so drop one item to
                            # make room then put the sentinel (≤2 iterations — the
                            # writer is the only consumer).
                            while True:
                                try:
                                    chunk_q.put_nowait(None)
                                    break
                                except queue.Full:
                                    pass
                                try:
                                    chunk_q.get_nowait()
                                except queue.Empty:
                                    pass
                            # MUST wait for the writer to fully exit before
                            # unwinding: the outer ``except`` unlinks ``temp_file``,
                            # which would race the writer's open file handle. See
                            # ``_await_writer_exit`` for why a plain join doesn't do.
                            await _await_writer_exit(writer_thread)
                            raise

                        _reject_empty_download(total_bytes)

                        os.replace(temp_file, output_file)
                        logger.debug(
                            "Downloaded %s%s (%d bytes)",
                            display_host,
                            parsed.path,
                            total_bytes,
                        )
                        return output_path
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    raise ArtifactDownloadError(
                        "media",
                        details=(
                            f"Authentication required for {display_host}{parsed.path}"
                            " -- try `notebooklm login`"
                        ),
                        cause=e,
                        status_code=e.response.status_code,
                    ) from e
                raise ArtifactDownloadError(
                    "media",
                    details=f"HTTP error downloading {display_host}{parsed.path}",
                    cause=e,
                    status_code=e.response.status_code,
                ) from e
            except httpx.RequestError as e:
                raise ArtifactDownloadError(
                    "media",
                    details=f"Network error downloading {display_host}{parsed.path}",
                    cause=e,
                ) from e
        except BaseException:
            temp_file.unlink(missing_ok=True)
            raise


__all__ = ["AssetDownloadService", "CredentialPolicy", "DownloadResult"]
