"""Source operations API."""

import asyncio
import builtins
import logging
import re
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from ._core import ClientCore
from ._url_utils import is_youtube_url
from .rpc import UPLOAD_URL, RPCError, RPCMethod
from .rpc.types import SourceStatus
from .types import (
    Source,
    SourceAddError,
    SourceFulltext,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
)

logger = logging.getLogger(__name__)


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

    async def list(self, notebook_id: str) -> list[Source]:
        """List all sources in a notebook.

        Args:
            notebook_id: The notebook ID.

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
            logger.debug("Empty or invalid notebook response when listing sources")
            return []

        nb_info = notebook[0]
        if not isinstance(nb_info, list) or len(nb_info) <= 1:
            logger.debug("Invalid notebook structure: expected list with sources at index 1")
            return []

        sources_list = nb_info[1]
        if not isinstance(sources_list, list):
            logger.debug("Sources list is not a list type: %s", type(sources_list))
            return []

        # Convert raw source data to Source objects
        sources = []
        for src in sources_list:
            if isinstance(src, list) and len(src) > 0:
                # Extract basic info from source structure
                src_id = src[0][0] if isinstance(src[0], list) else src[0]
                title = src[1] if len(src) > 1 else None

                # Extract URL if present (at src[2][7])
                url = None
                if len(src) > 2 and isinstance(src[2], list) and len(src[2]) > 7:
                    url_list = src[2][7]
                    if isinstance(url_list, list) and len(url_list) > 0:
                        url = url_list[0]

                # Extract timestamp from src[2][2] - [seconds, nanoseconds]
                created_at = None
                if len(src) > 2 and isinstance(src[2], list) and len(src[2]) > 2:
                    timestamp_list = src[2][2]
                    if isinstance(timestamp_list, list) and len(timestamp_list) > 0:
                        try:
                            created_at = datetime.fromtimestamp(timestamp_list[0])
                        except (TypeError, ValueError):
                            pass

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
                raise SourceProcessingError(source_id, source.status)

            # Don't sleep longer than remaining time
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
    ) -> Source:
        """Add a file source to a notebook using resumable upload.

        Uses Google's resumable upload protocol:
        1. Register source intent with RPC → get SOURCE_ID
        2. Start upload session with SOURCE_ID (get upload URL)
        3. Stream upload file content (memory-efficient for large files)

        Args:
            notebook_id: The notebook ID.
            file_path: Path to the file to upload.
            mime_type: MIME type of the file (not used in current implementation).
            wait: If True, wait for source to be ready before returning.
            wait_timeout: Maximum seconds to wait if wait=True (default: 120).

        Returns:
            The created Source object. If wait=False, status may be PROCESSING.

        Supported file types:
            - PDF: application/pdf
            - Text: text/plain
            - Markdown: text/markdown
            - Word: application/vnd.openxmlformats-officedocument.wordprocessingml.document
        """
        logger.debug("Adding file source to notebook %s: %s", notebook_id, file_path)
        file_path = Path(file_path).resolve()

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        if not file_path.is_file():
            raise ValueError(f"Not a regular file: {file_path}")

        filename = file_path.name
        # Get file size without loading into memory
        file_size = file_path.stat().st_size

        # Step 1: Register source intent with RPC → get SOURCE_ID
        source_id = await self._register_file_source(notebook_id, filename)

        # Step 2: Start resumable upload with the SOURCE_ID from step 1
        upload_url = await self._start_resumable_upload(notebook_id, filename, file_size, source_id)

        # Step 3: Stream upload file content (memory-efficient)
        await self._upload_file_streaming(upload_url, file_path)

        # Return source with the ID we got from registration
        # Note: _type_code is None because the actual type is determined
        # by the API after processing (PDF, TEXT, IMAGE, etc.)
        # Use wait=True or get() to retrieve the actual type after processing
        source = Source(
            id=source_id,
            title=filename,
            _type_code=None,  # Placeholder until processed
        )

        if wait:
            return await self.wait_until_ready(notebook_id, source.id, timeout=wait_timeout)

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
            [[source_data]],
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
        # False means stale, True means fresh
        return result is True

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

        # Parse response structure: [[null, [summary], [keywords]]]
        summary = ""
        keywords: list[str] = []

        if result and isinstance(result, list) and len(result) > 0:
            inner = result[0]
            if isinstance(inner, list):
                # Summary at [1][0]
                if len(inner) > 1 and isinstance(inner[1], list) and len(inner[1]) > 0:
                    summary = inner[1][0] if isinstance(inner[1][0], str) else ""
                # Keywords at [2][0]
                if len(inner) > 2 and isinstance(inner[2], list) and len(inner[2]) > 0:
                    keywords = inner[2][0] if isinstance(inner[2][0], list) else []

        return {"summary": summary, "keywords": keywords}

    async def get_fulltext(self, notebook_id: str, source_id: str) -> SourceFulltext:
        """Get the full indexed text content of a source.

        Returns the raw text content that was extracted and indexed from the source,
        along with metadata. This is what NotebookLM uses for chat and artifact generation.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to get fulltext for.

        Returns:
            SourceFulltext object with content, title, source_type, url, and char_count.

        Raises:
            SourceNotFoundError: If the source is not found or returns no data.

        Note:
            Source type codes: 1=google_docs, 2=google_other, 3=pdf, 4=pasted_text,
            5=web_page, 8=generated_text, 9=youtube
        """
        # GET_SOURCE RPC with params: [[source_id], [2], [2]]
        params = [[source_id], [2], [2]]
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

                # Source type at result[0][2][4]
                if len(result[0]) > 2 and isinstance(result[0][2], list):
                    if len(result[0][2]) > 4:
                        source_type = result[0][2][4]

                    # URL at result[0][2][7][0]
                    if len(result[0][2]) > 7 and isinstance(result[0][2][7], list):
                        if len(result[0][2][7]) > 0:
                            url = result[0][2][7][0]

            # Content blocks at result[3][0]
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

        raise ValueError("Failed to get SOURCE_ID from registration response")

    async def _start_resumable_upload(
        self,
        notebook_id: str,
        filename: str,
        file_size: int,
        source_id: str,
    ) -> str:
        """Start a resumable upload session and get the upload URL."""
        import json

        url = f"{UPLOAD_URL}?authuser=0"

        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Cookie": self._core.auth.cookie_header,
            "Origin": "https://notebooklm.google.com",
            "Referer": "https://notebooklm.google.com/",
            "x-goog-authuser": "0",
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

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=headers, content=body)
            response.raise_for_status()

            upload_url = response.headers.get("x-goog-upload-url")
            if not upload_url:
                raise ValueError("Failed to get upload URL from response headers")

            return upload_url

    async def _upload_file_streaming(self, upload_url: str, file_path: Path) -> None:
        """Stream upload file content to the resumable upload URL.

        Uses streaming to avoid loading the entire file into memory,
        which is important for large PDFs and documents.

        Args:
            upload_url: The resumable upload URL from _start_resumable_upload.
            file_path: Path to the file to upload.
        """
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Cookie": self._core.auth.cookie_header,
            "Origin": "https://notebooklm.google.com",
            "Referer": "https://notebooklm.google.com/",
            "x-goog-authuser": "0",
            "x-goog-upload-command": "upload, finalize",
            "x-goog-upload-offset": "0",
        }

        # Stream the file content instead of loading it all into memory
        async def file_stream():
            with open(file_path, "rb") as f:
                while chunk := f.read(65536):  # 64KB chunks
                    yield chunk

        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(upload_url, headers=headers, content=file_stream())
            response.raise_for_status()
