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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import ParseResult, urlparse

import httpx

from .._auth.cookies import load_httpx_cookies
from .._mind_maps_api import extract_interactive_tree_leaf
from .._row_adapters.notes import NoteRow
from ..exceptions import UnknownRPCMethodError, ValidationError
from ..rpc import ArtifactTypeCode, RPCMethod, safe_index
from ..types import (
    Artifact,
    ArtifactDownloadError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    ArtifactType,
)
from ._redirect_guard import redirect_revalidation_hooks
from .formatters import _extract_app_data, _format_interactive_content, _parse_data_table

if TYPE_CHECKING:
    from .._mind_map import NoteBackedMindMapService
    from .._row_adapters.artifacts import ArtifactRow
    from .._runtime.contracts import RpcCaller
    from .listing import ArtifactListingService

logger = logging.getLogger(__name__)

_TRUSTED_DOWNLOAD_DOMAINS = (".google.com", ".googleusercontent.com", ".googleapis.com")
# Bounded queue between the async chunk producer and the single writer
# thread. Small enough to provide back-pressure (the producer awaits when
# the writer falls behind) but large enough to keep the writer hot across
# a brief read stall. 8 slots × 64 KiB ≈ 512 KiB of in-flight buffering.
_DOWNLOAD_WRITER_QUEUE_SIZE = 8
# ``_PREFETCH_NOTE`` — referenced by the per-method docstrings below. Each
# ``download_<x>`` accepts an optional pre-fetched list (``artifacts_data`` raw
# studio rows / ``artifacts`` typed list / ``mind_maps`` note-backed rows). When
# supplied — the ``_app`` executor lists once to select the target and threads
# what it already fetched — the method skips its own otherwise-redundant second
# ``LIST_ARTIFACTS`` / ``GET_NOTES_AND_MIND_MAPS`` RPC; ``None`` re-lists as before
# (issue #1488).


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


def _is_trusted_download_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    # Match the EXACT host httpx connects to. httpx does NOT percent-decode
    # ``request.url.host``, so the guard must not either: decoding made
    # ``evil%2egoogleapis.com`` (%2e -> '.') read as trusted while the
    # connection went to the raw non-Google host (#1521). A real Google host
    # never contains ``%``, so reject any (defense-in-depth w/ the slash guards).
    hostname = hostname.lower()
    if "%" in hostname or "\\" in hostname or "/" in hostname:
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
        cookie_loader: Callable[[Any], Any] = _load_httpx_cookies,
    ) -> None:
        self._rpc = rpc
        self._listing = listing
        self._mind_maps = mind_maps
        self._storage_path, self._cookie_loader = storage_path, cookie_loader

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
        """Fetch interactive artifact HTML through the runtime RPC seam.

        ``GET_INTERACTIVE_HTML`` is the live generic ``GetArtifact`` getter; here
        we read the HTML body at ``[0][9][0]`` (quiz / flashcard content).
        """
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

    async def _get_interactive_mind_map_tree(
        self, notebook_id: str, artifact_id: str
    ) -> str | None:
        """Fetch the interactive mind-map JSON tree string.

        The interactive (studio-artifact) mind map exposes its ``{"name",
        "children"}`` node tree at ``[0][9][3]`` of the ``GET_INTERACTIVE_HTML``
        response (vs the HTML body at ``[0][9][0]``). Returns the raw JSON
        string, or ``None`` when the response is empty / not yet populated.
        """
        result = await self._rpc.rpc_call(
            RPCMethod.GET_INTERACTIVE_HTML,
            [artifact_id],
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        # ``extract_interactive_tree_leaf`` re-raises ``UnknownRPCMethodError``
        # on genuine ``[0][9]`` shape drift (failing loud like the sibling HTML
        # accessor ``_get_artifact_content``) while tolerating an absent ``[3]``
        # leaf as the legitimate "tree not populated yet" window (issue #1270).
        tree_json = extract_interactive_tree_leaf(
            result, source="_artifact_downloads._get_interactive_mind_map_tree"
        )
        return tree_json if isinstance(tree_json, str) else None

    async def download_audio(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: list[Any] | None = None,
    ) -> str:
        """Download an Audio Overview to a file (``artifacts_data``: see ``_PREFETCH_NOTE``)."""
        if artifacts_data is None:
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
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: list[Any] | None = None,
    ) -> str:
        """Download a Video Overview to a file (``artifacts_data``: see ``_PREFETCH_NOTE``)."""
        if artifacts_data is None:
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
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: list[Any] | None = None,
    ) -> str:
        """Download an Infographic to a file (``artifacts_data``: see ``_PREFETCH_NOTE``)."""
        if artifacts_data is None:
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
        *,
        artifacts_data: list[Any] | None = None,
    ) -> str:
        """Download a slide deck as PDF or PPTX (``artifacts_data``: see ``_PREFETCH_NOTE``)."""
        if output_format not in ("pdf", "pptx"):
            raise ValidationError(f"Invalid format '{output_format}'. Must be 'pdf' or 'pptx'.")

        if artifacts_data is None:
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
        *,
        artifacts: list[Artifact] | None = None,
    ) -> str:
        """Download quiz or flashcard artifact.

        ``artifacts`` is the optional pre-fetched *typed* list of the matching
        ``list_type`` (see ``_PREFETCH_NOTE``); this method still filters it to
        completed entries and id-matches within it.
        """
        valid_formats = ("json", "markdown", "html")
        if output_format not in valid_formats:
            raise ValidationError(
                f"Invalid output_format: {output_format!r}. Use one of: {', '.join(valid_formats)}"
            )

        is_quiz = artifact_type == "quiz"
        default_title = "Untitled Quiz" if is_quiz else "Untitled Flashcards"
        list_type = ArtifactType.QUIZ if is_quiz else ArtifactType.FLASHCARDS

        if artifacts is None:
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
            artifact, *_ = (
                completed  # typed Artifact list head (newest-first); unpack avoids name[int]
            )

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
        *,
        artifacts_data: list[Any] | None = None,
    ) -> str:
        """Download a report artifact as markdown (``artifacts_data``: see ``_PREFETCH_NOTE``)."""
        if artifacts_data is None:
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
        *,
        mind_maps: list[Any] | None = None,
        artifacts_data: list[Any] | None = None,
    ) -> str:
        """Download a mind map as JSON (note-backed or interactive kind).

        ``mind_maps`` (note-backed rows) and ``artifacts_data`` (raw studio rows,
        used only by the interactive-mind-map branch) are optional pre-fetched
        lists; see ``_PREFETCH_NOTE``. Each is fetched on demand when ``None``.
        """
        mind_maps_service = self._mind_maps

        # Fetch the note-backed list first: it is the primary backing for this
        # method, so an explicit id that resolves here (the happy path) avoids
        # the extra _list_raw artifact-collection network call entirely.
        if mind_maps is None:
            mind_maps = await mind_maps_service.list_mind_maps(notebook_id)

        # The JSON tree string to write — sourced from the note content for
        # note-backed maps, or from GET_INTERACTIVE_HTML for interactive ones.
        json_string: str | None = None

        if artifact_id:
            # Read the row id through the ``NoteRow`` adapter seam rather than a
            # raw ``mm[0]`` index so a numeric / non-str id is ``str``-coerced
            # consistently with the rest of the mind-map path (issue #1270) and
            # any future row-shape change is absorbed in one place.
            mind_map = next((mm for mm in mind_maps if NoteRow(mm).id == artifact_id), None)
            if mind_map is not None:
                json_string = mind_maps_service.extract_content(mind_map)
            else:
                # The id is not a note-backed mind map. Interactive
                # (studio-artifact) mind maps live in the artifact collection,
                # not the note-backed list — fetch the tree there so both kinds
                # download to the same JSON shape (issue #1256). Reuse the
                # caller-provided ``artifacts_data`` when present to avoid a
                # redundant second ``LIST_ARTIFACTS``.
                if artifacts_data is None:
                    artifacts_data = await self._list_raw(notebook_id)
                interactive = False
                for row in artifacts_data:
                    if not isinstance(row, list):
                        continue
                    artifact = Artifact.from_api_response(row)
                    if artifact.id == artifact_id and artifact.is_interactive_mind_map:
                        interactive = True
                        break
                if interactive:
                    json_string = await self._get_interactive_mind_map_tree(
                        notebook_id, artifact_id
                    )
                    if json_string is None:
                        # Found the interactive artifact but its tree is not yet
                        # readable (generation still settling).
                        raise ArtifactNotReadyError("mind_map")
                elif not mind_maps:
                    # Not interactive either: preserve the prior error precedence
                    # — an empty note-backed list reads as "not ready", a
                    # populated list with no matching id reads as "not found".
                    raise ArtifactNotReadyError("mind_map")
                else:
                    raise ArtifactNotFoundError(artifact_id, artifact_type="mind_map")
        else:
            # No explicit id: the first note-backed mind map (if any) is used.
            if not mind_maps:
                raise ArtifactNotReadyError("mind_map")
            json_string = mind_maps_service.extract_content(next(iter(mind_maps)))

        try:
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
        *,
        artifacts_data: list[Any] | None = None,
    ) -> str:
        """Download a data table as CSV (``artifacts_data``: see ``_PREFETCH_NOTE``)."""
        if artifacts_data is None:
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
        *,
        artifacts: list[Artifact] | None = None,
    ) -> str:
        """Download quiz questions."""
        return await self.download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "quiz", artifacts=artifacts
        )

    async def download_flashcards(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
        *,
        artifacts: list[Artifact] | None = None,
    ) -> str:
        """Download flashcard deck."""
        return await self.download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "flashcards", artifacts=artifacts
        )

    async def download_urls_batch(self, urls_and_paths: list[tuple[str, str]]) -> DownloadResult:
        """Download multiple files using httpx with proper cookie handling."""
        result = DownloadResult()

        cookies = await asyncio.to_thread(self._cookie_loader, self._storage_path)

        async with httpx.AsyncClient(
            cookies=cookies,
            follow_redirects=True,
            timeout=60.0,
            event_hooks=redirect_revalidation_hooks(_is_trusted_download_host),  # #1521
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
            cookies = await asyncio.to_thread(self._cookie_loader, self._storage_path)
            timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0)

            try:
                async with httpx.AsyncClient(  # noqa: SIM117
                    cookies=cookies,
                    follow_redirects=True,
                    timeout=timeout,
                    event_hooks=redirect_revalidation_hooks(_is_trusted_download_host),  # #1521
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
