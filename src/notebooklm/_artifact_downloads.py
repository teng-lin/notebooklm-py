"""Private artifact download service implementation."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import queue
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import ParseResult, unquote, urlparse

import httpx

from ._artifact_formatters import _extract_app_data, _format_interactive_content, _parse_data_table
from .auth import load_httpx_cookies
from .exceptions import UnknownRPCMethodError, ValidationError
from .rpc import ArtifactTypeCode, RPCMethod, safe_index
from .types import (
    Artifact,
    ArtifactDownloadError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    ArtifactType,
)

if TYPE_CHECKING:
    from ._artifact_listing import ArtifactListingService
    from ._mind_map import NoteBackedMindMapService
    from ._row_adapters_artifacts import ArtifactRow
    from ._runtime_contracts import RpcCaller

logger = logging.getLogger(__name__)

_TRUSTED_DOWNLOAD_DOMAINS = (".google.com", ".googleusercontent.com", ".googleapis.com")

# Bounded queue between the async chunk producer and the single writer
# thread. Small enough to provide back-pressure (the producer awaits when
# the writer falls behind) but large enough to keep the writer hot across
# a brief read stall. 8 slots × 64 KiB ≈ 512 KiB of in-flight buffering.
_DOWNLOAD_WRITER_QUEUE_SIZE = 8


async def _await_writer_exit(
    writer_thread: threading.Thread,
    *,
    re_raise_cancel: bool = False,
) -> None:
    """Wait for a download writer thread to actually exit.

    Plain ``await asyncio.to_thread(thread.join)`` is unsafe under
    cancellation: if our awaiting task is cancelled, the await raises
    ``CancelledError`` and we unwind even though the underlying
    ``thread.join`` is still blocked on the thread. The thread keeps
    running, which means the outer cleanup (``temp_file.unlink``)
    races with the writer's still-open file handle.

    ``asyncio.shield`` alone doesn't fix this: it keeps the *inner*
    join task alive across cancellation, but the *await* still raises
    ``CancelledError`` and we unwind anyway. The fix is a shield-loop
    that keeps re-awaiting the same shielded join task until it
    actually completes. Repeated cancellations only delay our
    re-raise, never the writer's exit.

    Cancellation handling:

    * Only ``asyncio.CancelledError`` is caught inside the loop — any
      other exception from the shielded join (currently none in
      practice, since ``Thread.join`` doesn't raise) propagates
      immediately so we don't accidentally hide a real bug.
    * The most recent ``CancelledError`` (if any) is preserved.
    * If ``re_raise_cancel=True``, the helper re-raises that
      ``CancelledError`` after the writer has fully exited. Callers
      on the success path want this so an in-flight cancellation
      isn't lost when the writer happens to finish first. Callers on
      a cleanup-path (the producer's ``except`` block, which already
      has an exception to re-raise) leave it at the default
      ``False`` so we don't mask the original error with a second
      cancellation.

    Handles both the original join-vs-unlink race AND the follow-up case
    where an initial fix could silently absorb task cancellation.
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
    `failed`: (url, exception) tuples for transient httpx / ValueError failures.
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


def _is_trusted_download_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    hostname = unquote(hostname).lower()
    if "\\" in hostname or "/" in hostname:
        return False
    return any(
        hostname == domain.lstrip(".") or hostname.endswith(domain)
        for domain in _TRUSTED_DOWNLOAD_DOMAINS
    )


def _download_display_host(parsed: ParseResult) -> str:
    if parsed.hostname is not None:
        return parsed.hostname
    return parsed.netloc.rsplit("@", 1)[-1]


class ArtifactDownloadService:
    """Download operations extracted from :class:`ArtifactsAPI`."""

    def __init__(
        self,
        *,
        rpc: RpcCaller,
        listing: ArtifactListingService,
        mind_maps: NoteBackedMindMapService,
        storage_path: Path | None = None,
    ) -> None:
        self._rpc = rpc
        self._listing = listing
        self._mind_maps = mind_maps
        self._storage_path = storage_path

    async def _list_raw(self, notebook_id: str) -> list[Any]:
        """List raw artifacts through the injected listing service."""
        return await self._listing.list_raw(notebook_id, rpc=self._rpc)

    async def _list_mind_maps(self, notebook_id: str) -> list[Any]:
        """List mind-map artifacts through the injected mind-map service."""
        return await self._mind_maps.list_mind_maps(notebook_id)

    async def _list_artifacts(
        self,
        notebook_id: str,
        artifact_type: ArtifactType,
    ) -> list[Artifact]:
        """List typed artifacts using the download service's patchable seams."""
        return await self._listing.list_artifacts(
            notebook_id,
            artifact_type,
            list_raw=self._list_raw,
            list_mind_maps=self._list_mind_maps,
        )

    def _select_artifact(
        self,
        candidates: list[Any],
        artifact_id: str | None,
        type_name: str,
        no_result_error_key: str,
        *,
        type_code: ArtifactTypeCode,
    ) -> ArtifactRow:
        """Select one completed artifact candidate as an adapter row."""
        return self._listing.select_completed_artifact_row(
            candidates,
            artifact_id,
            type_name,
            no_result_error_key,
            type_code=type_code,
        )

    async def _get_artifact_content(self, notebook_id: str, artifact_id: str) -> str | None:
        """Fetch interactive artifact HTML through the runtime RPC seam."""
        result = await self._rpc.rpc_call(
            RPCMethod.GET_INTERACTIVE_HTML,
            [artifact_id],
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        if result is None:
            return None
        return safe_index(
            result,
            0,
            9,
            0,
            method_id=RPCMethod.GET_INTERACTIVE_HTML.value,
            source="_artifact_downloads._get_artifact_content",
        )

    async def download_audio(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download an Audio Overview to a file."""
        artifacts_data = await self._list_raw(notebook_id)

        audio_art = self._select_artifact(
            artifacts_data,
            artifact_id,
            "Audio",
            "audio",
            type_code=ArtifactTypeCode.AUDIO,
        )

        try:
            url = audio_art.audio_url
        except UnknownRPCMethodError as e:
            raise ArtifactParseError(
                "audio",
                artifact_id=artifact_id,
                details=f"Failed to parse structure: {e}",
                cause=e,
            ) from e
        if not url:
            raise ArtifactParseError(
                "audio",
                artifact_id=artifact_id,
                details="Could not extract download URL from artifact metadata",
            )

        return await self.download_url(url, output_path)

    async def download_video(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download a Video Overview to a file."""
        artifacts_data = await self._list_raw(notebook_id)

        # Note: distinct error keys preserved — specific-ID miss raises
        # "video" (from type_name="Video"); empty-list raises
        # "video_overview" (from type_name_lower).
        video_art = self._select_artifact(
            artifacts_data,
            artifact_id,
            "Video",
            "video_overview",
            type_code=ArtifactTypeCode.VIDEO,
        )

        try:
            url = video_art.video_url
        except UnknownRPCMethodError as e:
            raise ArtifactParseError(
                "video_artifact",
                artifact_id=artifact_id,
                details=f"Failed to parse structure: {e}",
                cause=e,
            ) from e
        if not url:
            raise ArtifactParseError(
                "video_artifact",
                artifact_id=artifact_id,
                details="Could not extract download URL from artifact metadata",
            )

        return await self.download_url(url, output_path)

    async def download_infographic(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download an Infographic to a file."""
        artifacts_data = await self._list_raw(notebook_id)

        info_art = self._select_artifact(
            artifacts_data,
            artifact_id,
            "Infographic",
            "infographic",
            type_code=ArtifactTypeCode.INFOGRAPHIC,
        )

        try:
            url = info_art.infographic_url
            if not url:
                raise ArtifactParseError(
                    "infographic",
                    artifact_id=artifact_id,
                    details="Could not find metadata",
                )
            return await self.download_url(url, output_path)

        except (IndexError, TypeError, UnknownRPCMethodError) as e:
            raise ArtifactParseError(
                "infographic",
                artifact_id=artifact_id,
                details=f"Failed to parse structure: {e}",
                cause=e,
            ) from e

    async def download_slide_deck(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "pdf",
    ) -> str:
        """Download a slide deck as PDF or PPTX."""
        if output_format not in ("pdf", "pptx"):
            raise ValidationError(f"Invalid format '{output_format}'. Must be 'pdf' or 'pptx'.")

        artifacts_data = await self._list_raw(notebook_id)

        slide_art = self._select_artifact(
            artifacts_data,
            artifact_id,
            "Slide deck",
            "slide_deck",
            type_code=ArtifactTypeCode.SLIDE_DECK,
        )

        try:
            if output_format == "pptx":
                url = slide_art.slide_deck_pptx_url
                if not url:
                    raise ArtifactDownloadError(
                        "slide_deck", details="PPTX URL not available in artifact data"
                    )
            else:
                url = slide_art.slide_deck_pdf_url
                if not url:
                    raise ArtifactDownloadError(
                        "slide_deck",
                        details=f"Could not find {output_format.upper()} download URL",
                    )

        except UnknownRPCMethodError as e:
            raise ArtifactParseError(
                "slide_deck",
                artifact_id=artifact_id,
                details=f"Failed to parse structure: {e}",
                cause=e,
            ) from e

        return await self.download_url(url, output_path)

    async def download_interactive_artifact(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None,
        output_format: str,
        artifact_type: str,
    ) -> str:
        """Download quiz or flashcard artifact."""
        valid_formats = ("json", "markdown", "html")
        if output_format not in valid_formats:
            raise ValidationError(
                f"Invalid output_format: {output_format!r}. Use one of: {', '.join(valid_formats)}"
            )

        is_quiz = artifact_type == "quiz"
        default_title = "Untitled Quiz" if is_quiz else "Untitled Flashcards"
        list_type = ArtifactType.QUIZ if is_quiz else ArtifactType.FLASHCARDS

        artifacts = await self._list_artifacts(notebook_id, list_type)
        completed = [a for a in artifacts if a.is_completed]
        if not completed:
            raise ArtifactNotReadyError(artifact_type)

        completed.sort(key=lambda a: a.created_at.timestamp() if a.created_at else 0, reverse=True)

        if artifact_id:
            artifact = next((a for a in completed if a.id == artifact_id), None)
            if not artifact:
                raise ArtifactNotFoundError(artifact_id, artifact_type=artifact_type)
        else:
            artifact = completed[0]

        html_content = await self._get_artifact_content(notebook_id, artifact.id)
        if not html_content:
            raise ArtifactDownloadError(artifact_type, details="Failed to fetch content")

        try:
            app_data = _extract_app_data(html_content)
        except json.JSONDecodeError as e:
            raise ArtifactParseError(
                artifact_type, details=f"Failed to parse content: {e}", cause=e
            ) from e

        title = artifact.title or default_title
        content = _format_interactive_content(app_data, title, output_format, html_content, is_quiz)

        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        def _write_file() -> None:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)

        await asyncio.to_thread(_write_file)
        return output_path

    async def download_report(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a report artifact as markdown."""
        artifacts_data = await self._list_raw(notebook_id)

        report_art = self._select_artifact(
            artifacts_data,
            artifact_id,
            "Report",
            "report",
            type_code=ArtifactTypeCode.REPORT,
        )

        try:
            markdown_content = report_art.report_markdown

            if not isinstance(markdown_content, str):
                raise ArtifactParseError(
                    "report_content",
                    artifact_id=artifact_id,
                    details="Invalid structure",
                )

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)

            def _write_markdown() -> None:
                output.write_text(markdown_content, encoding="utf-8")

            await asyncio.to_thread(_write_markdown)
            return str(output)

        except (IndexError, TypeError, UnknownRPCMethodError) as e:
            raise ArtifactParseError(
                "report",
                artifact_id=artifact_id,
                details=f"Failed to parse structure: {e}",
                cause=e,
            ) from e

    async def download_mind_map(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a mind map as JSON."""
        mind_maps_service = self._mind_maps

        # Fetch the note-backed list first: it is the primary backing for this
        # method, so an explicit id that resolves here (the happy path) avoids
        # the extra _list_raw artifact-collection network call entirely.
        mind_maps = await mind_maps_service.list_mind_maps(notebook_id)

        if artifact_id:
            mind_map = next((mm for mm in mind_maps if mm[0] == artifact_id), None)
            if mind_map is None:
                # The id is not a note-backed mind map. Interactive
                # (studio-artifact) mind maps live in the artifact collection,
                # not the note-backed list — check there so an interactive id
                # gets a clear "not supported here" message instead of the
                # misleading ArtifactNotReadyError / ArtifactNotFoundError below.
                studio_rows = await self._list_raw(notebook_id)
                for row in studio_rows:
                    if not isinstance(row, list):
                        continue
                    artifact = Artifact.from_api_response(row)
                    if artifact.id == artifact_id and artifact.is_interactive_mind_map:
                        raise ArtifactDownloadError(
                            "interactive_mind_map",
                            artifact_id=artifact_id,
                            details=(
                                "interactive mind maps are not downloadable via "
                                "download_mind_map yet; unified mind-map support is pending"
                            ),
                        )
                # Not interactive either: preserve the prior error precedence —
                # an empty note-backed list reads as "not ready", a populated
                # list with no matching id reads as "not found".
                if not mind_maps:
                    raise ArtifactNotReadyError("mind_map")
                raise ArtifactNotFoundError(artifact_id, artifact_type="mind_map")
        else:
            # No explicit id: the first note-backed mind map (if any) is used.
            if not mind_maps:
                raise ArtifactNotReadyError("mind_map")
            mind_map = mind_maps[0]

        try:
            json_string = mind_maps_service.extract_content(mind_map)
            if json_string is None:
                raise ArtifactParseError("mind_map_content", details="Invalid structure")

            json_data = json.loads(json_string)

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)

            def _write_json() -> None:
                with output.open("w", encoding="utf-8") as f:
                    json.dump(json_data, f, indent=2, ensure_ascii=False)

            await asyncio.to_thread(_write_json)
            return str(output)

        except (IndexError, TypeError, json.JSONDecodeError) as e:
            raise ArtifactParseError(
                "mind_map", details=f"Failed to parse structure: {e}", cause=e
            ) from e

    async def download_data_table(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a data table as CSV."""
        artifacts_data = await self._list_raw(notebook_id)

        table_art = self._select_artifact(
            artifacts_data,
            artifact_id,
            "Data table",
            # Unified to "data_table" so both empty-list and explicit-id-miss
            # paths raise ArtifactNotReadyError with the same artifact_type key.
            "data_table",
            type_code=ArtifactTypeCode.DATA_TABLE,
        )

        try:
            raw_data = table_art.data_table_raw_payload
            headers, rows = _parse_data_table(raw_data)

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)

            def _write_csv() -> None:
                with output.open("w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
                    writer.writerows(rows)

            await asyncio.to_thread(_write_csv)

            return str(output)

        except (IndexError, TypeError, ValueError, UnknownRPCMethodError) as e:
            raise ArtifactParseError(
                "data_table",
                artifact_id=artifact_id,
                details=f"Failed to parse structure: {e}",
                cause=e,
            ) from e

    async def download_quiz(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
    ) -> str:
        """Download quiz questions."""
        return await self.download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "quiz"
        )

    async def download_flashcards(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
    ) -> str:
        """Download flashcard deck."""
        return await self.download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "flashcards"
        )

    async def download_urls_batch(self, urls_and_paths: list[tuple[str, str]]) -> DownloadResult:
        """Download multiple files using httpx with proper cookie handling."""
        result = DownloadResult()

        cookies = await asyncio.to_thread(_load_httpx_cookies, self._storage_path)

        async with httpx.AsyncClient(
            cookies=cookies,
            follow_redirects=True,
            timeout=60.0,
        ) as client:
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

                    response = await client.get(url)
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
            cookies = await asyncio.to_thread(_load_httpx_cookies, self._storage_path)
            timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0)

            try:
                async with httpx.AsyncClient(  # noqa: SIM117
                    cookies=cookies,
                    follow_redirects=True,
                    timeout=timeout,
                ) as client:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()

                        content_type = response.headers.get("content-type", "")
                        if "text/html" in content_type:
                            raise ArtifactDownloadError(
                                "media",
                                details="Download failed: received HTML instead of media file. "
                                "Authentication may have expired. Run 'notebooklm login'.",
                            )

                        # Producer/consumer split: a single dedicated
                        # writer thread drains a bounded queue and writes
                        # to ``temp_file``. Compared to the legacy
                        # per-chunk ``asyncio.to_thread(f.write, chunk)``,
                        # this avoids thousands of thread-pool allocations
                        # for multi-GB downloads.
                        #
                        # The writer runs on a dedicated ``threading.Thread``
                        # rather than ``asyncio.to_thread`` so it does NOT
                        # tie up a slot in asyncio's default executor pool.
                        # With many concurrent downloads, default-executor
                        # saturation by long-lived writers (each blocking
                        # on ``chunk_q.get()``) could deadlock producers
                        # trying to ``put`` via ``to_thread``.
                        #
                        # Producer puts use ``put_nowait`` first and only
                        # fall back to ``to_thread(put, ...)`` when the
                        # queue is full, minimizing default-executor
                        # pressure during normal flow.
                        #
                        # End-of-stream is signalled with a ``None``
                        # sentinel. Writer-side failures are surfaced via
                        # ``writer_error`` and an early ``writer_failed``
                        # ``threading.Event`` so the producer can short-
                        # circuit BEFORE the writer's drain completes,
                        # avoiding wasted network reads.
                        chunk_q: queue.Queue[bytes | None] = queue.Queue(
                            maxsize=_DOWNLOAD_WRITER_QUEUE_SIZE
                        )
                        writer_failed = threading.Event()
                        writer_error: list[BaseException] = []

                        def _writer_loop() -> None:
                            # If the writer raises (e.g. OSError on
                            # ``fh.write``), the bounded queue may have a
                            # producer parked in ``q.put`` waiting for a
                            # consumer. Without draining, that producer
                            # hangs forever because we are the only
                            # consumer. The ``finally`` drains pending
                            # items via ``get_nowait`` so blocked puts
                            # complete and the producer can observe the
                            # failure signal on its next iteration.
                            #
                            # ``writer_failed`` is set in the ``except``
                            # BEFORE the drain so the producer's
                            # short-circuit check fires as early as
                            # possible — the drain itself can run for a
                            # few milliseconds clearing the queue, during
                            # which the producer would otherwise read and
                            # discard network bytes pointlessly.
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
                                    # Writer raised mid-stream. Stop
                                    # reading — further network bytes
                                    # would just be discarded by the
                                    # drain. The original error is
                                    # re-raised via ``writer_error[0]``
                                    # below.
                                    break
                                # ``put_nowait`` avoids a ``to_thread``
                                # round-trip when the queue has space
                                # (the common case under balanced flow);
                                # fall back to ``to_thread`` only when
                                # the queue is full so the loop suspends
                                # cleanly under back-pressure.
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
                            # Join surfaces any exception the writer
                            # captured. ``_await_writer_exit`` shield-
                            # loops until the writer actually exits so
                            # the outer cleanup never races with the
                            # still-open file handle. ``re_raise_cancel
                            # =True`` ensures a cancellation that
                            # arrived while we were waiting for the
                            # writer isn't lost when the writer happens
                            # to finish first.
                            await _await_writer_exit(writer_thread, re_raise_cancel=True)
                            if writer_error:
                                raise writer_error[0]
                        except BaseException:
                            # On producer-side failure (network error,
                            # cancellation, HTML payload), make sure the
                            # writer sees a sentinel and exits — even if
                            # the queue is currently saturated. A bare
                            # ``put_nowait(None)`` would raise
                            # ``queue.Full`` and leave the writer parked
                            # in ``q.get`` forever; instead drop one item
                            # to make room, then put the sentinel. At
                            # most two iterations are needed: the writer
                            # is the only consumer, so once a slot opens
                            # nothing else refills it.
                            while True:
                                try:
                                    chunk_q.put_nowait(None)
                                    break
                                except queue.Full:
                                    pass
                                try:
                                    chunk_q.get_nowait()
                                except queue.Empty:
                                    # Writer drained between our put and
                                    # get — the next put attempt will
                                    # succeed.
                                    pass
                            # MUST wait for the writer to actually exit
                            # before unwinding: the outer ``except``
                            # unlinks ``temp_file``, which would race
                            # with the writer's still-open file handle
                            # otherwise. A plain
                            # ``contextlib.suppress(BaseException) +
                            # await to_thread(.join)`` does NOT suffice
                            # — the await itself can be re-cancelled and
                            # unwind before the writer finishes. The
                            # shield-loop in ``_await_writer_exit``
                            # keeps re-awaiting the same shielded join
                            # task across repeated cancellations until
                            # the writer thread is actually dead.
                            await _await_writer_exit(writer_thread)
                            raise

                        if total_bytes == 0:
                            raise ArtifactDownloadError(
                                "media",
                                details=(
                                    "Download produced 0 bytes -- the remote file may "
                                    "be missing or empty"
                                ),
                            )

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
