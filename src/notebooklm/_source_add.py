"""Private non-file source creation service."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs

from ._idempotency import idempotent_create
from .exceptions import (
    AuthError,
    NetworkError,
    NonIdempotentRetryError,
    RateLimitError,
    ServerError,
    SourceAddError,
)
from .rpc import RPCError, RPCMethod
from .types import Source

RpcCall = Callable[..., Awaitable[Any]]
ListSources = Callable[[str], Awaitable[list[Source]]]
WaitUntilReady = Callable[..., Awaitable[Source]]
RawSourceAdder = Callable[[str, str], Awaitable[Any]]
ParseUrl = Callable[[str], Any]
ExtractVideoId = Callable[[Any, str], str | None]
ValidateVideoId = Callable[[str], bool]
YoutubeDetector = Callable[[str], bool]


class SourceAddService:
    """URL, YouTube, text, and Drive source creation behavior."""

    async def add_url(
        self,
        notebook_id: str,
        url: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        add_youtube_source: RawSourceAdder,
        add_url_source: RawSourceAdder,
        list_sources: ListSources,
        wait_until_ready: WaitUntilReady,
        extract_youtube_video_id: Callable[[str], str | None],
        is_youtube_url: YoutubeDetector,
        logger: logging.Logger,
    ) -> Source:
        """Add a URL source to a notebook."""
        logger.debug("Adding URL source to notebook %s: %s", notebook_id, url[:80])
        video_id = extract_youtube_video_id(url)
        if not video_id and is_youtube_url(url):
            logger.warning(
                "URL appears to be YouTube but no video ID found: %s. "
                "Adding as web page - content may be incomplete. "
                "If this is a video URL, please report this as a bug.",
                url[:100],
            )

        async def _create() -> Source:
            # Preserve transport-level signals so callers can act on the
            # specific type (AuthError -> re-login, RateLimitError -> back-off
            # with retry_after, ServerError -> transient retry). RateLimitError,
            # ServerError, and NetworkError must propagate so idempotent_create
            # can catch them and run the probe. AuthError continues to
            # propagate to the caller because an auth failure cannot have
            # committed the write.
            try:
                if video_id:
                    result = await add_youtube_source(notebook_id, url)
                else:
                    result = await add_url_source(notebook_id, url)
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as e:
                raise SourceAddError(url, cause=e) from e

            if result is None:
                raise SourceAddError(url, message=f"API returned no data for URL: {url}")
            return Source.from_api_response(result)

        async def _probe() -> Source | None:
            try:
                sources = await list_sources(notebook_id)
            except Exception:
                logger.debug(
                    "add_url: probe list() failed; treating as no match",
                    exc_info=True,
                )
                return None
            for source in sources:
                if source.url == url:
                    return source
            return None

        source = await idempotent_create(
            _create,
            _probe,
            label=f"sources.add_url[{url[:40]}]",
        )

        if wait:
            return await wait_until_ready(notebook_id, source.id, timeout=wait_timeout)

        return source

    async def add_text(
        self,
        notebook_id: str,
        title: str,
        content: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        idempotent: bool = False,
        rpc_call: RpcCall,
        wait_until_ready: WaitUntilReady,
        logger: logging.Logger,
    ) -> Source:
        """Add a text source to a notebook."""
        if idempotent:
            raise NonIdempotentRetryError(
                "add_text cannot be marked idempotent: text sources have no "
                "reliable server-side dedupe key (titles non-unique, content "
                "not exposed). For idempotent text imports, embed a UUID in "
                "the title and dedupe client-side. See "
                "docs/python-api.md#idempotency."
            )
        logger.debug("Adding text source to notebook %s: %s", notebook_id, title)
        params = [
            [[None, [title, content], None, None, None, None, None, None]],
            notebook_id,
            [2],
            None,
            None,
        ]
        try:
            result = await rpc_call(
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
            return await wait_until_ready(notebook_id, source.id, timeout=wait_timeout)

        return source

    async def add_drive(
        self,
        notebook_id: str,
        file_id: str,
        title: str,
        *,
        mime_type: str = "application/vnd.google-apps.document",
        wait: bool = False,
        wait_timeout: float = 120.0,
        rpc_call: RpcCall,
        wait_until_ready: WaitUntilReady,
        logger: logging.Logger,
    ) -> Source:
        """Add a Google Drive document as a source."""
        logger.debug("Adding Drive source to notebook %s: %s", notebook_id, title)
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
            [source_data],
            notebook_id,
            [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]
        result = await rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        if result is None:
            raise SourceAddError(title, message=f"API returned no data for Drive source: {title}")
        source = Source.from_api_response(result)

        if wait:
            return await wait_until_ready(notebook_id, source.id, timeout=wait_timeout)

        return source

    def extract_youtube_video_id(
        self,
        url: str,
        *,
        parse_url: ParseUrl,
        extract_video_id_from_parsed_url: ExtractVideoId,
        is_valid_video_id: ValidateVideoId,
        logger: logging.Logger,
    ) -> str | None:
        """Extract a YouTube video ID from supported URL formats."""
        try:
            parsed = parse_url(url.strip())
            hostname = (parsed.hostname or "").lower()

            youtube_domains = {
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
                "music.youtube.com",
                "youtu.be",
            }

            if hostname not in youtube_domains:
                return None

            video_id = extract_video_id_from_parsed_url(parsed, hostname)

            if video_id and is_valid_video_id(video_id):
                return video_id

            return None

        except (AttributeError, TypeError, ValueError) as e:
            logger.debug("Failed to parse YouTube URL '%s': %s", url[:100], e)
            return None

    def extract_video_id_from_parsed_url(self, parsed: Any, hostname: str) -> str | None:
        """Extract the raw YouTube video ID from a parsed URL."""
        if hostname == "youtu.be":
            path = parsed.path.lstrip("/")
            if path:
                return path.split("/")[0].strip()
            return None

        path_prefixes = ("shorts", "embed", "live", "v")
        path_segments = parsed.path.lstrip("/").split("/")

        if len(path_segments) >= 2 and path_segments[0].lower() in path_prefixes:
            return path_segments[1].strip()

        if parsed.query:
            query_params = parse_qs(parsed.query)
            v_param = query_params.get("v", [])
            if v_param and v_param[0]:
                return v_param[0].strip()

        return None

    def is_valid_video_id(self, video_id: str) -> bool:
        """Validate YouTube video ID format."""
        return bool(video_id and re.match(r"^[a-zA-Z0-9_-]+$", video_id))

    async def add_youtube_source(
        self,
        notebook_id: str,
        url: str,
        *,
        rpc_call: RpcCall,
    ) -> Any:
        """Add a YouTube video as a source."""
        params = [
            [[None, None, None, None, None, None, None, [url], None, None, 1]],
            notebook_id,
            [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]
        return await rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=False,
            disable_internal_retries=True,
        )

    async def add_url_source(
        self,
        notebook_id: str,
        url: str,
        *,
        rpc_call: RpcCall,
    ) -> Any:
        """Add a regular URL as a source."""
        params = [
            [[None, None, [url], None, None, None, None, None]],
            notebook_id,
            [2],
            None,
            None,
        ]
        return await rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            disable_internal_retries=True,
        )


__all__ = ["SourceAddService"]
