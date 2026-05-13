"""Source operations API."""

import asyncio
import builtins
import logging
import re
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import httpx

from ._core import ClientCore
from ._env import get_base_url
from ._url_utils import is_youtube_url
from .auth import authuser_query, format_authuser_value
from .exceptions import NetworkError, ValidationError
from .rpc import RPCError, RPCMethod, get_upload_url
from .rpc.types import SourceStatus
from .types import (
    Source,
    SourceAddError,
    SourceFulltext,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
    _extract_source_created_at,
    _extract_source_url,
)

logger = logging.getLogger(__name__)


# Source type codes where status=3 (ERROR) is transient rather than
# terminal. Audio/media (10) and unclassified (None / 0) sources can
# briefly report status=3 during transcription/classification before
# settling at status=2. All other types (PDFs, web, YouTube, etc.) treat
# status=3 as a terminal failure. New unknown types default to terminal
# — fail fast rather than silently looping until timeout. See #391.
_TRANSIENT_ERROR_TYPES: tuple[int | None, ...] = (10, 0, None)


class SourcesAPI:
    """Operations on NotebookLM sources.

    Provides methods for adding, listing, getting, deleting, renaming,
    and refreshing sources in notebooks.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            sources = await client.sources.list(notebook_id)
            new_src = await client.sources.add_url(notebook_id, "https://example.com")
            await client.sources.rename(notebook_id, new_src.id, "Better Title")
    """

    def __init__(self, core: ClientCore):
        """Initialize the sources API.

        Args:
            core: The core client infrastructure.
        """
        self._core = core

    @staticmethod
    def _handle_malformed_list_response(
        notebook_id: str,
        message: str,
        *log_args: object,
        strict: bool,
        error_detail: str = "API response structure changed",
    ) -> list[Source]:
        logger.warning("SourcesAPI.list: " + message, notebook_id, *log_args)
        if strict:
            raise RPCError(f"Could not list sources for {notebook_id}: {error_detail}")
        return []

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
        # Get notebook data which includes sources
        params = [notebook_id, None, [2], None, 0]
        notebook = await self._core.rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        if not notebook or not isinstance(notebook, list) or len(notebook) == 0:
            return self._handle_malformed_list_response(
                notebook_id,
                "Empty or invalid notebook response when listing sources for %s "
                "(API response structure may have changed)",
                strict=strict,
            )

        nb_info = notebook[0]
        if not isinstance(nb_info, list) or len(nb_info) <= 1:
            return self._handle_malformed_list_response(
                notebook_id,
                "Unexpected notebook structure for %s: expected list with sources at index 1 "
                "(API structure may have changed)",
                strict=strict,
            )

        sources_list = nb_info[1]
        if not isinstance(sources_list, list):
            return self._handle_malformed_list_response(
                notebook_id,
                "Sources data for %s is not a list (type=%s), returning empty list "
                "(API structure may have changed)",
                type(sources_list).__name__,
                strict=strict,
                error_detail=f"sources data is {type(sources_list).__name__}, not list",
            )

        # Convert raw source data to Source objects
        sources = []
        for src in sources_list:
            if isinstance(src, list) and len(src) > 0:
                # Extract basic info from source structure
                src_id = src[0][0] if isinstance(src[0], list) else src[0]
                title = src[1] if len(src) > 1 else None

                # Extract URL via the shared helper. GET_NOTEBOOK source entries
                # use the same medium-nested metadata shape as
                # Source.from_api_response, which doesn't support the bare-http
                # [0] fallback (metadata[0] can pack unrelated data). Precedence
                # is restricted to [7] > [5]; keep the two call sites aligned.
                url = _extract_source_url(src[2] if len(src) > 2 else None, allow_bare_http=False)

                # Extract timestamp from src[2][2] - [seconds, nanoseconds]
                created_at = _extract_source_created_at(src[2] if len(src) > 2 else None)

                # Extract status from src[3][1]
                # See SourceStatus enum for valid values
                status = SourceStatus.READY  # Default to ready
                if len(src) > 3 and isinstance(src[3], list) and len(src[3]) > 1:
                    status_code = src[3][1]
                    if status_code in (
                        SourceStatus.PROCESSING,
                        SourceStatus.READY,
                        SourceStatus.ERROR,
                        SourceStatus.PREPARING,
                    ):
                        status = status_code

                # Extract source type code from src[2][4]
                # See SourceType enum for valid values
                type_code = None
                if len(src) > 2 and isinstance(src[2], list) and len(src[2]) > 4:
                    tc = src[2][4]
                    if isinstance(tc, int):
                        type_code = tc

                sources.append(
                    Source(
                        id=str(src_id),
                        title=title,
                        url=url,
                        _type_code=type_code,
                        created_at=created_at,
                        status=status,
                    )
                )

        return sources

    async def get(self, notebook_id: str, source_id: str) -> Source | None:
        """Get details of a specific source.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID.

        Returns:
            Source object with current status, or None if not found.
        """
        # GET_SOURCE RPC (hizoJc) appears to be unreliable for source metadata lookup,
        # especially for newly created sources. It returns None or incomplete data.
        # Fallback to filtering from list() which uses GET_NOTEBOOK (rLM1Ne)
        # and reliably returns all sources with their status/types.
        sources = await self.list(notebook_id)
        for source in sources:
            if source.id == source_id:
                return source
        return None

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
        start = monotonic()
        interval = initial_interval
        last_status: int | None = None

        while True:
            # Check timeout before each poll
            elapsed = monotonic() - start
            if elapsed >= timeout:
                raise SourceTimeoutError(source_id, timeout, last_status)

            source = await self.get(notebook_id, source_id)

            if source is None:
                raise SourceNotFoundError(source_id)

            last_status = source.status

            if source.is_ready:
                return source

            if source.is_error:
                if source._type_code not in _TRANSIENT_ERROR_TYPES:
                    raise SourceProcessingError(source_id, source.status)
                # For audio (type 10) or unclassified (None / 0) sources,
                # status=3 can be a transient state during transcription —
                # keep polling instead of treating it as terminal. See #391.

            # Don't sleep longer than remaining time
            remaining = timeout - (monotonic() - start)
            if remaining <= 0:
                raise SourceTimeoutError(source_id, timeout, last_status)

            sleep_time = min(interval, remaining)
            await asyncio.sleep(sleep_time)
            interval = min(interval * backoff_factor, max_interval)

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
        start = monotonic()
        interval = initial_interval
        last_status: int | None = None

        while True:
            elapsed = monotonic() - start
            if elapsed >= timeout:
                raise SourceTimeoutError(source_id, timeout, last_status)

            source = await self.get(notebook_id, source_id)

            if source is not None:
                last_status = source.status

                if source.is_error:
                    if source._type_code not in _TRANSIENT_ERROR_TYPES:
                        raise SourceProcessingError(source_id, source.status)
                    # Transient ERROR for audio (type 10) or unclassified
                    # (None / 0) — keep polling. See #391.
                else:
                    # Any non-error status (PROCESSING, READY, PREPARING)
                    # means the source is registered server-side; we're done.
                    return source

            remaining = timeout - (monotonic() - start)
            if remaining <= 0:
                raise SourceTimeoutError(source_id, timeout, last_status)

            sleep_time = min(interval, remaining)
            await asyncio.sleep(sleep_time)
            interval = min(interval * backoff_factor, max_interval)

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
        tasks = [
            self.wait_until_ready(notebook_id, sid, timeout=timeout, **kwargs) for sid in source_ids
        ]
        return list(await asyncio.gather(*tasks))

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
        logger.debug("Adding URL source to notebook %s: %s", notebook_id, url[:80])
        video_id = self._extract_youtube_video_id(url)
        try:
            if video_id:
                result = await self._add_youtube_source(notebook_id, url)
            else:
                # Warn if URL looks like YouTube but we couldn't extract video ID
                if is_youtube_url(url):
                    logger.warning(
                        "URL appears to be YouTube but no video ID found: %s. "
                        "Adding as web page - content may be incomplete. "
                        "If this is a video URL, please report this as a bug.",
                        url[:100],
                    )
                result = await self._add_url_source(notebook_id, url)
        except RPCError as e:
            # Wrap RPC error with more helpful context for users
            raise SourceAddError(url, cause=e) from e

        if result is None:
            raise SourceAddError(url, message=f"API returned no data for URL: {url}")
        source = Source.from_api_response(result)

        if wait:
            return await self.wait_until_ready(notebook_id, source.id, timeout=wait_timeout)

        return source

    async def add_text(
        self,
        notebook_id: str,
        title: str,
        content: str,
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> Source:
        """Add a text source (copied text) to a notebook.

        Args:
            notebook_id: The notebook ID.
            title: Title for the source.
            content: Text content.
            wait: If True, wait for source to be ready before returning.
            wait_timeout: Maximum seconds to wait if wait=True (default: 120).

        Returns:
            The created Source object. If wait=False, status may be PROCESSING.
        """
        logger.debug("Adding text source to notebook %s: %s", notebook_id, title)
        params = [
            [[None, [title, content], None, None, None, None, None, None]],
            notebook_id,
            [2],
            None,
            None,
        ]
        try:
            result = await self._core.rpc_call(
                RPCMethod.ADD_SOURCE,
                params,
                source_path=f"/notebook/{notebook_id}",
            )
        except RPCError as e:
            raise SourceAddError(
                title,
                cause=e,
                message=f"Failed to add text source '{title}'",
            ) from e

        if result is None:
            raise SourceAddError(title, message=f"API returned no data for text source: {title}")

        source = Source.from_api_response(result)

        if wait:
            return await self.wait_until_ready(notebook_id, source.id, timeout=wait_timeout)

        return source

    async def add_file(
        self,
        notebook_id: str,
        file_path: str | Path,
        mime_type: str | None = None,
        wait: bool = False,
        wait_timeout: float = 120.0,
        *,
        title: str | None = None,
    ) -> Source:
        """Add a file source to a notebook using resumable upload.

        Uses Google's resumable upload protocol:
        1. Register source intent with RPC → get SOURCE_ID
        2. Start upload session with SOURCE_ID (get upload URL)
        3. Stream upload file content (memory-efficient for large files)
        4. Optionally rename the source if a custom ``title`` was supplied
           (the file-add RPC has no title slot, so a follow-up
           ``UPDATE_SOURCE`` is the only way to set one).

        Args:
            notebook_id: The notebook ID.
            file_path: Path to the file to upload.
            mime_type: MIME type of the file (not used in current implementation).
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

        Returns:
            The created Source object. If wait=False, status may be PROCESSING.

        Supported file types:
            - PDF: application/pdf
            - Text: text/plain
            - Markdown: text/markdown
            - EPUB: application/epub+zip
            - Word: application/vnd.openxmlformats-officedocument.wordprocessingml.document
        """
        logger.debug("Adding file source to notebook %s: %s", notebook_id, file_path)
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
        # Get file size without loading into memory
        file_size = file_path.stat().st_size

        # Step 1: Register source intent with RPC → get SOURCE_ID
        source_id = await self._register_file_source(notebook_id, filename)

        # Step 2: Start resumable upload with the SOURCE_ID from step 1
        upload_url = await self._start_resumable_upload(notebook_id, filename, file_size, source_id)

        # Step 3: Stream upload file content (memory-efficient)
        await self._upload_file_streaming(upload_url, file_path)

        # Step 4: Ensure the source is registered server-side BEFORE renaming.
        # The UPDATE_SOURCE RPC silently no-ops against an unregistered source
        # (returns success-shaped data, but the title change never propagates),
        # so a custom-title request must force a brief registration wait even
        # when the caller passed wait=False. See #388.
        #
        # When wait=True the caller asked for full processing; use
        # wait_until_ready. When only a custom title was supplied, use the
        # narrower wait_until_registered so wait=False callers don't pay the
        # full processing latency just to get a rename through.
        needs_title_rename = title is not None and title != filename
        if wait:
            source = await self.wait_until_ready(notebook_id, source_id, timeout=wait_timeout)
        elif needs_title_rename:
            # Honor the caller's wait_timeout directly — wait_until_registered
            # polls and returns on the first PROCESSING/READY status, so the
            # narrow wait still completes fast for typical sources even when
            # the upper bound is generous (e.g. long-audio callers passing 300s).
            source = await self.wait_until_registered(notebook_id, source_id, timeout=wait_timeout)
        else:
            # Fire-and-forget placeholder. _type_code is None because the
            # actual type is determined by the API after processing (PDF,
            # TEXT, IMAGE, etc.). status=PROCESSING reflects that the source
            # has been registered but not yet processed — callers can use
            # wait=True or get() to retrieve the resolved state.
            source = Source(
                id=source_id,
                title=filename,
                status=SourceStatus.PROCESSING,
                _type_code=None,  # Placeholder until processed
            )

        # Step 5: Apply custom title now that the source is registered. The
        # file-add RPC ignores any title hint, so a separate UPDATE_SOURCE
        # call is the only way to honor the caller's intent.
        if title is not None and title != filename:
            try:
                renamed = await self.rename(notebook_id, source_id, title)
                # Only merge the new title onto the waited source. rename()'s
                # response shape can be sparse (UPDATE_SOURCE sometimes returns
                # just an id + title) and would otherwise null out _type_code,
                # url, and created_at that wait_until_ready() populated. Fall
                # back to the requested title if rename's response omits it.
                source = replace(source, title=renamed.title or title)
            except (RPCError, NetworkError):
                # Don't fail the whole upload if the rename fails — the file is
                # already uploaded and registered. Surface a warning so the
                # caller can retry. The registered source (from the forced wait
                # above) is returned with its server-side title.
                logger.warning(
                    "Source %s uploaded but rename to %r failed",
                    source_id,
                    title,
                    exc_info=True,
                )

        return source

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
        logger.debug("Adding Drive source to notebook %s: %s", notebook_id, title)
        # Drive source structure: [[file_id, mime_type, 1, title], null x9, 1]
        source_data = [
            [file_id, mime_type, 1, title],
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            1,
        ]
        params = [
            [source_data],  # Single wrap, not double - matches web UI
            notebook_id,
            [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]
        result = await self._core.rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        source = Source.from_api_response(result)

        if wait:
            return await self.wait_until_ready(notebook_id, source.id, timeout=wait_timeout)

        return source

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
        try:
            parsed = urlparse(url.strip())
            hostname = (parsed.hostname or "").lower()

            # Check if this is a YouTube domain
            youtube_domains = {
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
                "music.youtube.com",
                "youtu.be",
            }

            if hostname not in youtube_domains:
                return None

            video_id = self._extract_video_id_from_parsed_url(parsed, hostname)

            if video_id and self._is_valid_video_id(video_id):
                return video_id

            return None

        except (AttributeError, TypeError, ValueError) as e:
            logger.debug("Failed to parse YouTube URL '%s': %s", url[:100], e)
            return None

    def _extract_video_id_from_parsed_url(self, parsed: Any, hostname: str) -> str | None:
        """Extract video ID from a parsed YouTube URL.

        Args:
            parsed: ParseResult from urlparse.
            hostname: Lowercase hostname.

        Returns:
            The raw video ID (not yet validated), or None.
        """
        # youtu.be short URLs: youtu.be/VIDEO_ID
        if hostname == "youtu.be":
            path = parsed.path.lstrip("/")
            if path:
                return path.split("/")[0].strip()
            return None

        # youtube.com path-based formats: /shorts/ID, /embed/ID, /live/ID, /v/ID
        path_prefixes = ("shorts", "embed", "live", "v")
        path_segments = parsed.path.lstrip("/").split("/")

        if len(path_segments) >= 2 and path_segments[0].lower() in path_prefixes:
            return path_segments[1].strip()

        # Query param: ?v=VIDEO_ID (for /watch URLs)
        if parsed.query:
            query_params = parse_qs(parsed.query)
            v_param = query_params.get("v", [])
            if v_param and v_param[0]:
                return v_param[0].strip()

        return None

    def _is_valid_video_id(self, video_id: str) -> bool:
        """Validate YouTube video ID format.

        YouTube video IDs contain only alphanumeric characters, hyphens,
        and underscores. They are typically 11 characters but can vary.

        Args:
            video_id: The video ID to validate.

        Returns:
            True if the video ID format is valid, False otherwise.
        """
        return bool(video_id and re.match(r"^[a-zA-Z0-9_-]+$", video_id))

    async def _add_youtube_source(self, notebook_id: str, url: str) -> Any:
        """Add a YouTube video as a source."""
        params = [
            [[None, None, None, None, None, None, None, [url], None, None, 1]],
            notebook_id,
            [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]
        return await self._core.rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def _add_url_source(self, notebook_id: str, url: str) -> Any:
        """Add a regular URL as a source."""
        params = [
            [[None, None, [url], None, None, None, None, None]],
            notebook_id,
            [2],
            None,
            None,
        ]
        return await self._core.rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def _register_file_source(self, notebook_id: str, filename: str) -> str:
        """Register a file source intent and get SOURCE_ID."""
        # Note: filename is double-nested: [[filename]], not triple-nested
        params = [
            [[filename]],
            notebook_id,
            [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]

        result = await self._core.rpc_call(
            RPCMethod.ADD_SOURCE_FILE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        # Parse SOURCE_ID from response - handle various nesting formats
        # API returns different structures: [[[[id]]]], [[[id]]], [[id]], etc.
        if result and isinstance(result, list):

            def extract_id(data):
                """Recursively extract first string from nested lists."""
                if isinstance(data, str):
                    return data
                if isinstance(data, list) and len(data) > 0:
                    return extract_id(data[0])
                return None

            source_id = extract_id(result)
            if source_id:
                return source_id

        raise SourceAddError(filename, message="Failed to get SOURCE_ID from registration response")

    async def _start_resumable_upload(
        self,
        notebook_id: str,
        filename: str,
        file_size: int,
        source_id: str,
    ) -> str:
        """Start a resumable upload session and get the upload URL."""
        import json

        auth_route = format_authuser_value(
            self._core.auth.authuser,
            self._core.auth.account_email,
        )
        base_url = get_base_url()
        url = (
            f"{get_upload_url()}?"
            f"{authuser_query(self._core.auth.authuser, self._core.auth.account_email)}"
        )

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

        # Pass the live cookie jar (not a flat Cookie header) so httpx scopes
        # cookies by Domain attribute, matching browser behavior. The /upload/_/
        # endpoint is served by Scotty, which validates host-sensitive cookies
        # (notably OSID) against the request host: an OSID issued for
        # myaccount.google.com leaked to notebooklm.google.com is rejected with
        # HTTP 500 and x-goog-upload-status: final. A real browser would never
        # send the foreign-host OSID; Domain-scoping the jar enforces the same.
        # Using get_http_client().cookies (instead of auth.cookie_jar) so we
        # pick up SIDCC/SIDTS rotations applied during the live session. See #373.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, read=60.0),
            cookies=self._core.get_http_client().cookies,
        ) as client:
            response = await client.post(url, headers=headers, content=body)
            response.raise_for_status()

            upload_url = response.headers.get("x-goog-upload-url")
            if not upload_url:
                raise SourceAddError(
                    filename, message="Failed to get upload URL from response headers"
                )

            return upload_url

    async def _upload_file_streaming(self, upload_url: str, file_path: Path) -> None:
        """Stream upload file content to the resumable upload URL.

        Uses streaming to avoid loading the entire file into memory,
        which is important for large PDFs and documents.

        Args:
            upload_url: The resumable upload URL from _start_resumable_upload.
            file_path: Path to the file to upload.
        """
        base_url = get_base_url()
        auth_route = format_authuser_value(
            self._core.auth.authuser,
            self._core.auth.account_email,
        )
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "x-goog-authuser": auth_route,
            "Origin": base_url,
            "Referer": f"{base_url}/",
            "x-goog-upload-command": "upload, finalize",
            "x-goog-upload-offset": "0",
        }

        # Stream the file content instead of loading it all into memory
        async def file_stream():
            with open(file_path, "rb") as f:
                while chunk := f.read(65536):  # 64KB chunks
                    yield chunk

        # See _start_resumable_upload: pass the live cookie jar so httpx scopes
        # cookies per Domain attribute. Scotty validates OSID against host
        # and rejects foreign-host cookies. (#373)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, read=300.0),
            cookies=self._core.get_http_client().cookies,
        ) as client:
            response = await client.post(upload_url, headers=headers, content=file_stream())
            response.raise_for_status()
