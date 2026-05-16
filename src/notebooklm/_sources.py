"""Source operations API."""

import asyncio
import builtins
import logging
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import IO, Any, Literal
from urllib.parse import urlparse

import httpx

from . import _source_upload
from ._capabilities import ClientCoreCapabilities
from ._core import ClientCore
from ._source_add import SourceAddService
from ._source_listing import SourceLister
from ._source_polling import SourcePoller
from ._source_upload import SourceUploadPipeline
from ._url_utils import is_youtube_url
from .rpc import RPCMethod
from .types import (
    Source,
    SourceFulltext,
    SourceNotFoundError,
    _extract_source_url,
)

logger = logging.getLogger(__name__)

_SOURCE_ID_UUID_PATTERN = _source_upload._SOURCE_ID_UUID_PATTERN
_extract_register_file_source_id = _source_upload._extract_register_file_source_id
_looks_like_id_string = _source_upload._looks_like_id_string


class SourcesAPI:
    """Operations on NotebookLM sources.

    Provides methods for adding, listing, getting, deleting, renaming,
    and refreshing sources in notebooks.

    Usage:
        async with await NotebookLMClient.from_storage() as client:
            sources = await client.sources.list(notebook_id)
            new_src = await client.sources.add_url(notebook_id, "https://example.com")
            await client.sources.rename(notebook_id, new_src.id, "Better Title")
    """

    def __init__(
        self,
        core: ClientCore,
        upload_timeout: httpx.Timeout | None = None,
    ):
        """Initialize the sources API.

        Args:
            core: The core client infrastructure.
            upload_timeout: Optional override for the ``httpx.Timeout`` used
                by the resumable-upload start handshake and the finalize
                POST. ``None`` (default) preserves the original hardcoded
                values (10.0s connect / 60.0s read for start; 10.0s connect
                / 300.0s read for finalize). The supplied ``Timeout`` is
                used wholesale at both sites — supplying ``httpx.Timeout(read=600.0)``
                leaves ``connect``/``write``/``pool`` at httpx's own 5.0s
                defaults, NOT the original 10.0s. Specify all components
                explicitly (e.g. ``httpx.Timeout(10.0, read=600.0)``) to
                avoid surprises.
        """
        self._core = core
        self._capabilities = ClientCoreCapabilities(core)
        self._adder = SourceAddService()
        self._lister = SourceLister(self._rpc_call)
        self._poller = SourcePoller()
        self._uploader = SourceUploadPipeline()
        self._upload_timeout = upload_timeout

    async def _rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
    ) -> Any:
        """Delegate through the current core RPC method for late-bound test overrides."""
        return await self._core.rpc_call(
            method,
            params,
            source_path=source_path,
            allow_null=allow_null,
            _is_retry=_is_retry,
            disable_internal_retries=disable_internal_retries,
        )

    def _resolve_upload_timeout(self, default: httpx.Timeout) -> httpx.Timeout:
        """Return the configured upload timeout, or ``default`` if unset."""
        return self._upload_timeout if self._upload_timeout is not None else default

    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Source]:
        """List all sources in a notebook.

        Args:
            notebook_id: The notebook ID.
            strict: Raise RPCError on malformed source-list responses instead
                of returning an empty list. Intended for internal flows where
                a malformed snapshot must not be treated as an empty notebook.

        Returns:
            List of Source objects.
        """
        return await self._lister.list(notebook_id, strict=strict)

    async def get(self, notebook_id: str, source_id: str) -> Source | None:
        """Get details of a specific source.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID.

        Returns:
            Source object with current status, or None if not found.
        """
        return await self._lister.get(
            notebook_id,
            source_id,
            list_sources=self.list,
        )

    async def wait_until_ready(
        self,
        notebook_id: str,
        source_id: str,
        timeout: float = 120.0,
        initial_interval: float = 1.0,
        max_interval: float = 10.0,
        backoff_factor: float = 1.5,
    ) -> Source:
        """Wait for a source to become ready.

        Polls the source status until it becomes READY or ERROR, or timeout.
        Uses exponential backoff to reduce API load.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to wait for.
            timeout: Maximum time to wait in seconds (default: 120).
            initial_interval: Initial polling interval in seconds (default: 1).
            max_interval: Maximum polling interval in seconds (default: 10).
            backoff_factor: Multiplier for polling interval (default: 1.5).

        Returns:
            The ready Source object.

        Raises:
            SourceTimeoutError: If timeout is reached before source is ready.
            SourceProcessingError: If source processing fails (status=ERROR).
            SourceNotFoundError: If source is not found in the notebook.

        Example:
            source = await client.sources.add_url(notebook_id, url)
            # Source may still be processing...
            ready_source = await client.sources.wait_until_ready(
                notebook_id, source.id
            )
            # Now safe to use in chat/artifacts
        """
        return await self._poller.wait_until_ready(
            notebook_id,
            source_id,
            timeout=timeout,
            initial_interval=initial_interval,
            max_interval=max_interval,
            backoff_factor=backoff_factor,
            get_source=self.get,
            sleep=asyncio.sleep,
            monotonic=monotonic,
            logger=logger,
        )

    async def wait_until_registered(
        self,
        notebook_id: str,
        source_id: str,
        timeout: float = 30.0,
        initial_interval: float = 0.5,
        max_interval: float = 5.0,
        backoff_factor: float = 1.5,
    ) -> Source:
        """Wait for a source to be registered server-side (status >= PROCESSING).

        Polls until the source is visible in the notebook listing and has a
        non-ERROR status (or, for audio/unclassified sources, a transient
        ERROR — see ``_TRANSIENT_ERROR_TYPES``). Returns as soon as the
        source exists, without waiting for full processing.

        This is intended for narrow follow-up RPCs like UPDATE_SOURCE that
        only require the source to be registered, not fully processed.
        Registration is fast (seconds) even for long audio sources, so the
        default timeout is much shorter than ``wait_until_ready``'s.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to wait for.
            timeout: Maximum time to wait in seconds (default: 30).
            initial_interval: Initial polling interval in seconds (default: 0.5).
            max_interval: Maximum polling interval in seconds (default: 5).
            backoff_factor: Multiplier for polling interval (default: 1.5).

        Returns:
            The registered Source object (status is PROCESSING, READY, or
            PREPARING).

        Raises:
            SourceTimeoutError: If timeout is reached before source is registered.
            SourceProcessingError: If source reports a terminal ERROR for a
                non-transient source type.
        """
        return await self._poller.wait_until_registered(
            notebook_id,
            source_id,
            timeout=timeout,
            initial_interval=initial_interval,
            max_interval=max_interval,
            backoff_factor=backoff_factor,
            get_source=self.get,
            sleep=asyncio.sleep,
            monotonic=monotonic,
            logger=logger,
        )

    async def wait_for_sources(
        self,
        notebook_id: str,
        source_ids: builtins.list[str],
        timeout: float = 120.0,
        **kwargs: Any,
    ) -> builtins.list[Source]:
        """Wait for multiple sources to become ready in parallel.

        Args:
            notebook_id: The notebook ID.
            source_ids: List of source IDs to wait for.
            timeout: Per-source timeout in seconds.
            **kwargs: Additional arguments passed to wait_until_ready().

        Returns:
            List of ready Source objects in the same order as source_ids.

        Raises:
            SourceTimeoutError: If any source times out.
            SourceProcessingError: If any source fails.
            SourceNotFoundError: If any source is not found.

        Example:
            sources = [
                await client.sources.add_url(nb_id, url1),
                await client.sources.add_url(nb_id, url2),
            ]
            ready_sources = await client.sources.wait_for_sources(
                nb_id, [s.id for s in sources]
            )
        """
        return await self._poller.wait_for_sources(
            notebook_id,
            source_ids,
            timeout=timeout,
            wait_until_ready=self.wait_until_ready,
            logger=logger,
            **kwargs,
        )

    async def add_url(
        self,
        notebook_id: str,
        url: str,
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> Source:
        """Add a URL source to a notebook.

        Automatically detects YouTube URLs and uses the appropriate method.

        Args:
            notebook_id: The notebook ID.
            url: The URL to add.
            wait: If True, wait for source to be ready before returning.
            wait_timeout: Maximum seconds to wait if wait=True (default: 120).

        Returns:
            The created Source object. If wait=False, status may be PROCESSING.

        Example:
            # Add and wait for processing
            source = await client.sources.add_url(nb_id, url, wait=True)

            # Or add without waiting (for batch operations)
            source = await client.sources.add_url(nb_id, url)
            # ... add more sources ...
            await client.sources.wait_for_sources(nb_id, [s.id for s in sources])
        """
        return await self._adder.add_url(
            notebook_id,
            url,
            wait=wait,
            wait_timeout=wait_timeout,
            add_youtube_source=self._add_youtube_source,
            add_url_source=self._add_url_source,
            list_sources=self.list,
            wait_until_ready=self.wait_until_ready,
            extract_youtube_video_id=self._extract_youtube_video_id,
            is_youtube_url=is_youtube_url,
            logger=logger,
        )

    async def add_text(
        self,
        notebook_id: str,
        title: str,
        content: str,
        wait: bool = False,
        wait_timeout: float = 120.0,
        *,
        idempotent: bool = False,
    ) -> Source:
        """Add a text source (copied text) to a notebook.

        Args:
            notebook_id: The notebook ID.
            title: Title for the source.
            content: Text content.
            wait: If True, wait for source to be ready before returning.
            wait_timeout: Maximum seconds to wait if wait=True (default: 120).
            idempotent: T7.B2 — opt-in safety flag that REFUSES the call
                rather than risk silent duplication on retry. Text sources
                lack a reliable server-side dedupe key (titles non-unique;
                content not exposed in the source list), so the
                probe-then-retry pattern used by ``add_url`` cannot be
                applied here. When True, raises
                :class:`NonIdempotentRetryError` immediately. Default
                ``False`` preserves historical behavior (the underlying
                ``_perform_authed_post`` 5xx / 429 / network retry loop
                still runs and can duplicate the resource on a retry that
                follows a server-side commit). For idempotent text imports,
                embed a UUID in the title and dedupe client-side. See
                ``docs/python-api.md#idempotency``.

        Returns:
            The created Source object. If wait=False, status may be PROCESSING.

        Raises:
            NonIdempotentRetryError: When ``idempotent=True``.
        """
        return await self._adder.add_text(
            notebook_id,
            title,
            content,
            wait=wait,
            wait_timeout=wait_timeout,
            idempotent=idempotent,
            rpc_call=self._rpc_call,
            wait_until_ready=self.wait_until_ready,
            logger=logger,
        )

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
    ) -> Source:
        """Add a file source to a notebook using resumable upload.

        Uses Google's resumable upload protocol:
        1. Register source intent with RPC → get SOURCE_ID
        2. Start upload session with SOURCE_ID (get upload URL)
        3. Stream upload file content (memory-efficient for large files)
        4. Optionally rename the source if a custom ``title`` was supplied
           (the file-add RPC has no title slot, so a follow-up
           ``UPDATE_SOURCE`` is the only way to set one).

        Concurrency / FD lifecycle (T7.D3 / audit §23):
            The upload section runs under
            ``ClientCore.get_upload_semaphore()`` which bounds simultaneous
            in-flight uploads at ``max_concurrent_uploads`` (default 4).
            Each in-flight upload holds **one open file descriptor** for
            the duration of the upload, so the cap doubles as an
            FD-exhaustion guard. The file is opened ONCE during validation
            and the resulting FD is held across the size-check, RPC
            registration, upload-session start, and streamed body POST —
            closing the TOCTOU window where the path could have been
            replaced between two separate ``open()`` calls. A
            ``try``/``with`` guarantees the FD is released on every exit
            path, including ``CancelledError``.

        Args:
            notebook_id: The notebook ID.
            file_path: Path to the file to upload.
            mime_type: Deprecated; unused. Retained as a positional argument for
                backward compatibility. The MIME type is inferred server-side
                from the filename extension. Passing a non-None value emits a
                ``DeprecationWarning``. Slated for removal in a future minor
                release (see ``# DEPRECATION-REMOVAL: v0.X.0`` below).
            title: Optional display title. When provided and different from the
                source filename, a rename is issued after upload so the source
                appears with this title in the UI and API responses. Leading and
                trailing whitespace is stripped; empty titles are rejected. If
                the post-upload rename fails, the upload is preserved, a warning
                is logged, and the returned source keeps the filename title.

                Important: supplying a non-default title forces a brief
                registration wait (~seconds) for the source to become visible
                server-side *before* the rename is issued, even when
                ``wait=False``. The UPDATE_SOURCE RPC silently no-ops against
                an unregistered source, so blocking here is the only way to
                honor the caller's intent. This narrow wait completes once
                the source's status is non-ERROR (or transient-ERROR for
                audio); it does NOT wait for full processing. See #388.
            wait: If True, wait for source to be fully ready before returning.
                Note that supplying ``title`` also forces a narrow pre-rename
                registration wait regardless of this flag — see the ``title``
                parameter above.
            wait_timeout: Maximum seconds to wait if ``wait=True``. Also bounds
                the narrow registration wait triggered by a custom ``title``;
                that wait returns on the first PROCESSING/READY poll so it
                completes in seconds for typical sources regardless of this
                value. Default: 120.
            on_progress: Optional sync or async callback invoked as
                ``on_progress(bytes_sent, total_bytes)`` during the streaming
                upload body. Callback exceptions propagate and abort the
                upload, matching normal application callback semantics.

        Returns:
            The created Source object. If wait=False, status may be PROCESSING.

        Supported file types:
            - PDF: application/pdf
            - Text: text/plain
            - Markdown: text/markdown
            - EPUB: application/epub+zip
            - Word: application/vnd.openxmlformats-officedocument.wordprocessingml.document
        """
        return await self._uploader.add_file(
            notebook_id,
            file_path,
            mime_type=mime_type,
            wait=wait,
            wait_timeout=wait_timeout,
            title=title,
            on_progress=on_progress,
            deprecation_warning_stacklevel=3,
            capabilities=self._capabilities,
            register_file_source=self._register_file_source,
            start_resumable_upload=self._start_resumable_upload,
            upload_file_streaming=self._upload_file_streaming,
            wait_until_ready=self.wait_until_ready,
            wait_until_registered=self.wait_until_registered,
            rename=self.rename,
            logger=logger,
        )

    async def add_drive(
        self,
        notebook_id: str,
        file_id: str,
        title: str,
        mime_type: str = "application/vnd.google-apps.document",
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> Source:
        """Add a Google Drive document as a source.

        Args:
            notebook_id: The notebook ID.
            file_id: The Google Drive file ID.
            title: Display title for the source.
            mime_type: MIME type of the Drive document. Common values:
                - application/vnd.google-apps.document (Google Docs)
                - application/vnd.google-apps.presentation (Slides)
                - application/vnd.google-apps.spreadsheet (Sheets)
                - application/pdf (PDF files in Drive)
            wait: If True, wait for source to be ready before returning.
            wait_timeout: Maximum seconds to wait if wait=True (default: 120).

        Returns:
            The created Source object. If wait=False, status may be PROCESSING.

        Example:
            from notebooklm.types import DriveMimeType

            source = await client.sources.add_drive(
                notebook_id,
                file_id="1abc123xyz",
                title="My Document",
                mime_type=DriveMimeType.GOOGLE_DOC.value,
                wait=True,  # Wait for processing
            )
        """
        return await self._adder.add_drive(
            notebook_id,
            file_id,
            title,
            mime_type=mime_type,
            wait=wait,
            wait_timeout=wait_timeout,
            rpc_call=self._rpc_call,
            wait_until_ready=self.wait_until_ready,
            logger=logger,
        )

    async def delete(self, notebook_id: str, source_id: str) -> bool:
        """Delete a source from a notebook.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to delete.

        Returns:
            True if deletion succeeded.
        """
        logger.debug("Deleting source %s from notebook %s", source_id, notebook_id)
        params = [[[source_id]]]
        await self._core.rpc_call(
            RPCMethod.DELETE_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return True

    async def rename(self, notebook_id: str, source_id: str, new_title: str) -> Source:
        """Rename a source.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to rename.
            new_title: The new title.

        Returns:
            Updated Source object.
        """
        logger.debug("Renaming source %s to: %s", source_id, new_title)
        params = [None, [source_id], [[[new_title]]]]
        result = await self._core.rpc_call(
            RPCMethod.UPDATE_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return Source.from_api_response(result) if result else Source(id=source_id, title=new_title)

    async def refresh(self, notebook_id: str, source_id: str) -> bool:
        """Refresh a source to get updated content (for URL/Drive sources).

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to refresh.

        Returns:
            True if refresh was initiated.
        """
        params = [None, [source_id], [2]]
        await self._core.rpc_call(
            RPCMethod.REFRESH_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return True

    async def check_freshness(self, notebook_id: str, source_id: str) -> bool:
        """Check if a source needs to be refreshed.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to check.

        Returns:
            True if source is fresh, False if it needs refresh.
        """
        params = [None, [source_id], [2]]
        result = await self._core.rpc_call(
            RPCMethod.CHECK_SOURCE_FRESHNESS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        # API returns different structures depending on source type:
        #   - [] (empty array): source is fresh (URL sources)
        #   - [[null, true, [source_id]]]: source is fresh (Drive sources)
        #   - True: source is fresh
        #   - False: source is stale
        if result is True:
            return True
        if result is False:
            return False
        if isinstance(result, list):
            # Empty array means fresh
            if len(result) == 0:
                return True
            # Check for nested structure [[null, true, ...]] from Drive sources
            first = result[0]
            if isinstance(first, list) and len(first) > 1 and first[1] is True:
                return True
        return False

    async def get_guide(self, notebook_id: str, source_id: str) -> dict[str, Any]:
        """Get AI-generated summary and keywords for a specific source.

        This is the "Source Guide" feature shown when clicking on a source
        in the NotebookLM UI.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to get guide for.

        Returns:
            Dictionary containing:
                - summary: AI-generated summary with **bold** keywords (markdown)
                - keywords: List of topic keyword strings
        """
        # Deeply nested source ID: [[[[source_id]]]]
        params = [[[[source_id]]]]
        result = await self._core.rpc_call(
            RPCMethod.GET_SOURCE_GUIDE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        # Parse response structure: [[[null, [summary], [[keywords]], []]]]
        # Real API returns 3 levels of nesting before the data array
        summary = ""
        keywords: list[str] = []

        if result and isinstance(result, list) and len(result) > 0:
            outer = result[0]
            if isinstance(outer, list) and len(outer) > 0:
                inner = outer[0]
                if isinstance(inner, list):
                    # Summary at [1][0]
                    if len(inner) > 1 and isinstance(inner[1], list) and len(inner[1]) > 0:
                        summary = inner[1][0] if isinstance(inner[1][0], str) else ""
                    # Keywords at [2][0]
                    if len(inner) > 2 and isinstance(inner[2], list) and len(inner[2]) > 0:
                        keywords = inner[2][0] if isinstance(inner[2][0], list) else []

        return {"summary": summary, "keywords": keywords}

    async def get_fulltext(
        self,
        notebook_id: str,
        source_id: str,
        *,
        output_format: Literal["text", "markdown"] = "text",
    ) -> SourceFulltext:
        """Get the full content of a source.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to get fulltext for.
            output_format: Content format - ``"text"`` (default) returns flattened
                plaintext, ``"markdown"`` returns the source with headings,
                tables, links, and emphasis preserved. The markdown format
                requires the ``markdownify`` package (``pip install
                'notebooklm-py[markdown]'``).

        Returns:
            SourceFulltext object with content, title, source_type, url, and char_count.

        Raises:
            SourceNotFoundError: If the source is not found or returns no data.

        Note:
            Source type codes: 1=google_docs, 2=google_other, 3=pdf, 4=pasted_text,
            5=web_page, 8=generated_text, 9=youtube

            The ``"markdown"`` format works by requesting the HTML rendition
            from the API (params ``[3],[3]`` instead of ``[2],[2]``) and
            converting it via *markdownify*.
        """
        if output_format not in ("text", "markdown"):
            raise ValueError(f"Invalid format: '{output_format}'. Must be 'text' or 'markdown'.")

        # Fail fast on missing optional dep so CLI users don't pay for an RPC
        # round-trip before seeing the install hint.
        if output_format == "markdown":
            try:
                from markdownify import markdownify as md
            except ImportError:
                raise ImportError(
                    "The 'markdown' format requires the 'markdownify' package. "
                    "Install it with: pip install 'notebooklm-py[markdown]'"
                ) from None

        # [3],[3] returns HTML at result[4][1]; [2],[2] returns plaintext at result[3][0]
        params = [[source_id], [3], [3]] if output_format == "markdown" else [[source_id], [2], [2]]

        result = await self._core.rpc_call(
            RPCMethod.GET_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        # Validate response - raise if source not found
        if not result or not isinstance(result, list):
            raise SourceNotFoundError(f"Source {source_id} not found in notebook {notebook_id}")

        # Parse response structure
        title = ""
        source_type = None
        url = None
        content = ""

        if result and isinstance(result, list):
            # Title at result[0][1]
            if len(result) > 0 and isinstance(result[0], list) and len(result[0]) > 1:
                title = result[0][1] if isinstance(result[0][1], str) else ""

                # Source type at result[0][2][4]; source URLs may be stored
                # at [7][0] for web/PDF sources or [5][0] for YouTube sources.
                if len(result[0]) > 2 and isinstance(result[0][2], list):
                    metadata = result[0][2]
                    if len(metadata) > 4:
                        source_type = metadata[4]
                    url = _extract_source_url(metadata, allow_bare_http=False)

            if output_format == "markdown":
                # HTML content at result[4][1]; may be absent for source types
                # without an HTML rendition (e.g. youtube, pasted_text).
                html_content = None
                if len(result) > 4 and isinstance(result[4], list) and len(result[4]) > 1:
                    candidate = result[4][1]
                    if isinstance(candidate, str):
                        html_content = candidate
                if html_content is not None:
                    content = md(html_content, heading_style="ATX")
                else:
                    logger.warning(
                        "Source %s (type=%s) has no HTML rendition for output_format='markdown'; "
                        "returning empty content. Retry with output_format='text'.",
                        source_id,
                        source_type,
                    )
            else:
                # Plaintext content blocks at result[3][0]
                # Each block may be nested arrays with text strings
                if len(result) > 3 and isinstance(result[3], list) and len(result[3]) > 0:
                    content_blocks = result[3][0]
                    if isinstance(content_blocks, list):
                        texts = self._extract_all_text(content_blocks)
                        content = "\n".join(texts)

        # Log warning if content is empty but source exists
        if not content:
            logger.warning(
                "Source %s returned empty content (type=%s, title=%s)",
                source_id,
                source_type,
                title,
            )

        return SourceFulltext(
            source_id=source_id,
            title=title,
            content=content,
            _type_code=source_type,
            url=url,
            char_count=len(content),
        )

    # =========================================================================
    # Private helper methods
    # =========================================================================

    def _extract_all_text(self, data: builtins.list, max_depth: int = 100) -> builtins.list[str]:
        """Recursively extract all text strings from nested arrays.

        Args:
            data: Nested list structure to extract text from.
            max_depth: Maximum recursion depth to prevent stack overflow.

        Returns:
            List of extracted text strings.
        """
        if max_depth <= 0:
            logger.warning("Max recursion depth reached in text extraction")
            return []

        texts: builtins.list[str] = []
        for item in data:
            if isinstance(item, str) and len(item) > 0:
                texts.append(item)
            elif isinstance(item, builtins.list):
                texts.extend(self._extract_all_text(item, max_depth - 1))
        return texts

    def _extract_youtube_video_id(self, url: str) -> str | None:
        """Extract YouTube video ID from various URL formats.

        Handles all common YouTube URL formats:
        - Standard: youtube.com/watch?v=VIDEO_ID (any query param order)
        - Short: youtu.be/VIDEO_ID
        - Shorts: youtube.com/shorts/VIDEO_ID
        - Embed: youtube.com/embed/VIDEO_ID
        - Live: youtube.com/live/VIDEO_ID
        - Legacy: youtube.com/v/VIDEO_ID
        - Mobile: m.youtube.com/watch?v=VIDEO_ID
        - Music: music.youtube.com/watch?v=VIDEO_ID

        Args:
            url: The URL to parse.

        Returns:
            The video ID if found and valid, None otherwise.
        """
        return self._adder.extract_youtube_video_id(
            url,
            parse_url=urlparse,
            extract_video_id_from_parsed_url=self._extract_video_id_from_parsed_url,
            is_valid_video_id=self._is_valid_video_id,
            logger=logger,
        )

    def _extract_video_id_from_parsed_url(self, parsed: Any, hostname: str) -> str | None:
        """Extract video ID from a parsed YouTube URL.

        Args:
            parsed: ParseResult from urlparse.
            hostname: Lowercase hostname.

        Returns:
            The raw video ID (not yet validated), or None.
        """
        return self._adder.extract_video_id_from_parsed_url(parsed, hostname)

    def _is_valid_video_id(self, video_id: str) -> bool:
        """Validate YouTube video ID format.

        YouTube video IDs contain only alphanumeric characters, hyphens,
        and underscores. They are typically 11 characters but can vary.

        Args:
            video_id: The video ID to validate.

        Returns:
            True if the video ID format is valid, False otherwise.
        """
        return self._adder.is_valid_video_id(video_id)

    async def _add_youtube_source(self, notebook_id: str, url: str) -> Any:
        """Add a YouTube video as a source.

        ``disable_internal_retries=True`` (T7.B2): ADD_SOURCE is a
        mutating RPC that may have committed server-side even if the
        client sees a 5xx / network error. The probe-then-retry loop
        in ``add_url`` owns recovery via ``idempotent_create``.
        """
        # allow_null=False mirrors _register_file_source — ADD_SOURCE on
        # success returns the new source row. A null result with a status
        # code at wrb.fr[5] is the #407 / #474 mode; allow_null=True would
        # swallow that diagnostic. The decoder now raises RPCError with the
        # status code so add_url can wrap it into SourceAddError with detail.
        return await self._adder.add_youtube_source(
            notebook_id,
            url,
            rpc_call=self._rpc_call,
        )

    async def _add_url_source(self, notebook_id: str, url: str) -> Any:
        """Add a regular URL as a source.

        ``disable_internal_retries=True`` (T7.B2): see
        ``_add_youtube_source`` for the rationale.
        """
        return await self._adder.add_url_source(
            notebook_id,
            url,
            rpc_call=self._rpc_call,
        )

    async def _register_file_source(self, notebook_id: str, filename: str) -> str:
        """Register a file source intent and get SOURCE_ID."""
        return await self._uploader.register_file_source(
            notebook_id,
            filename,
            rpc_call=self._rpc_call,
        )

    async def _start_resumable_upload(
        self,
        notebook_id: str,
        filename: str,
        file_size: int,
        source_id: str,
    ) -> str:
        """Start a resumable upload session and get the upload URL."""
        return await self._uploader.start_resumable_upload(
            notebook_id,
            filename,
            file_size,
            source_id,
            capabilities=self._capabilities,
            resolve_upload_timeout=self._resolve_upload_timeout,
            async_client_factory=httpx.AsyncClient,
        )

    async def _upload_file_streaming(
        self,
        upload_url: str,
        file_obj: IO[bytes] | Path,
        *,
        filename: str | None = None,
        on_progress: Callable[[int, int], object] | None = None,
        total_bytes: int | None = None,
    ) -> None:
        """Stream upload file content to the resumable upload URL.

        Uses streaming to avoid loading the entire file into memory,
        which is important for large PDFs and documents.

        File-descriptor contract (T7.D3 / audit §23):
          When called from ``add_file`` (the production path), ``file_obj``
          is an already-open ``IO[bytes]`` and this helper TAKES OWNERSHIP
          of the FD lifecycle: a done-callback on the shielded finalize
          task closes the FD when streaming completes — success, error,
          OR after the post-finalize background-drain branch from the
          cancellation contract below. Ownership transfer is required
          because the shielded background task may outlive the caller's
          ``add_file`` invocation under post-finalize cancel; if the
          caller closed the FD on cancel, the still-running background
          POST would read from a closed FD and abort, breaking the
          T7.C3 dangling-session guarantee.

          A legacy ``Path`` argument is still accepted; the helper opens
          + closes the FD itself in that branch. ``add_file`` never
          takes that path — the Path branch exists only for the
          existing direct-call unit tests in
          ``tests/unit/test_sources_upload.py``.

        Cancellation contract (T7.C3 / audit §9):
          - The finalize POST is wrapped in ``asyncio.shield``. If a
            ``CancelledError`` arrives while the finalize POST is in
            flight, the inner Task keeps running so the server-side
            session reaches a known terminal state instead of dangling.
            The cancel is then re-raised to the caller.
          - If the cancel arrives BEFORE the finalize POST is dispatched
            (e.g. while the local ``httpx.AsyncClient`` is being
            constructed), a best-effort ``X-Goog-Upload-Command: cancel``
            POST is fired against the same resumable upload URL via
            ``asyncio.create_task``. The cleanup task is not awaited —
            re-raising must not block on best-effort cleanup. The cleanup
            runs on a detached task with no outer await chain, so a
            caller-level cancel cannot reach it; no explicit shield is
            needed at that layer (see ``_cancel_upload_session`` docstring).

        Args:
            upload_url: The resumable upload URL from _start_resumable_upload.
            file_obj: An open binary file object positioned at the bytes to
                upload, or (legacy) a ``Path`` the helper will open itself.
                When ``add_file`` is the caller, this is always the open
                FD and OWNERSHIP TRANSFERS to this helper (see
                file-descriptor contract above). Passing a ``Path`` is
                only supported for direct unit tests that bypass
                ``add_file``.
            filename: Optional filename used for diagnostic logging.
                Defaults to ``"<file>"`` when not supplied.
            on_progress: Optional sync or async callback invoked as
                ``on_progress(bytes_sent, total_bytes)`` as chunks are yielded.
            total_bytes: Total bytes expected. Required for the add_file FD
                path; inferred from the path for legacy direct-call tests when
                omitted.
        """
        return await self._uploader.upload_file_streaming(
            upload_url,
            file_obj,
            filename=filename,
            on_progress=on_progress,
            total_bytes=total_bytes,
            capabilities=self._capabilities,
            resolve_upload_timeout=self._resolve_upload_timeout,
            async_client_factory=httpx.AsyncClient,
            cancel_upload_session=self._cancel_upload_session,
            logger=logger,
        )

    async def _cancel_upload_session(self, upload_url: str, base_url: str, auth_route: str) -> None:
        """Best-effort POST a Scotty resumable-upload cancel command.

        Invoked fire-and-forget (via ``asyncio.create_task``) from
        ``_upload_file_streaming`` when a ``CancelledError`` arrives
        BEFORE the finalize POST is dispatched, so the server-side
        session is torn down instead of held until Scotty's GC timeout.

        Network failures are swallowed — Ctrl-C cleanup is best-effort;
        the worst case is that the session lives until Scotty GCs it.
        Since the caller schedules this on a detached task, there is no
        outer await chain that can deliver a cancellation here, so no
        extra shield is needed at this layer.
        """
        await self._uploader.cancel_upload_session(
            upload_url,
            base_url,
            auth_route,
            capabilities=self._capabilities,
            async_client_factory=httpx.AsyncClient,
            logger=logger,
        )
