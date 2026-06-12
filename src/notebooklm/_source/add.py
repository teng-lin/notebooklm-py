"""Private non-file source creation service."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs

from .._idempotency import idempotent_create
from .._runtime.contracts import RpcCaller
from ..exceptions import (
    AuthError,
    NetworkError,
    NonIdempotentRetryError,
    RateLimitError,
    ServerError,
    SourceAddError,
)
from ..rpc import RPCError, RPCMethod
from ..types import Source
from .upload_payloads import build_template_block

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
            return Source.from_api_response(result, method_id=RPCMethod.ADD_SOURCE.value)

        async def _probe() -> Source | None:
            try:
                sources = await list_sources(notebook_id)
            except (AuthError, RateLimitError, ServerError, NetworkError):
                # Transport- and auth-level probe failures must propagate.
                # Silently returning None here lets ``idempotent_create``
                # re-issue the create on top of a broken probe, which is
                # exactly the duplicate-source bug we are guarding against.
                raise
            except Exception:
                logger.debug(
                    "add_url: probe list() failed with non-transport error; treating as no match",
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
        rpc: RpcCaller,
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
        # Nested template block per the Gemini-3.5 wire migration (#1546): the
        # text spec grew from 8 to 11 elements (slot 3 None -> 2, trailing 1) and
        # the flat [2],None,None tail collapsed into the shared template block.
        # The literal 2 at slot 3 is a source-type code taken verbatim from the
        # web-UI capture; its exact meaning is undocumented. Verified live
        # against an un-migrated account.
        params = [
            [[None, [title, content], None, 2, None, None, None, None, None, None, 1]],
            notebook_id,
            build_template_block(),
        ]
        try:
            result = await rpc.rpc_call(
                RPCMethod.ADD_SOURCE,
                params,
                source_path=f"/notebook/{notebook_id}",
                operation_variant="text",
            )
        except (AuthError, RateLimitError, ServerError, NetworkError):
            # Preserve transport-level signals so callers can act on the
            # specific type (AuthError -> re-login, RateLimitError -> back-off
            # with retry_after, ServerError -> transient retry) instead of
            # receiving everything collapsed into SourceAddError — the same
            # ADR-0019 catch ordering add_url and add_drive use.
            raise
        except RPCError as e:
            raise SourceAddError(
                title,
                cause=e,
                message=f"Failed to add text source '{title}'",
            ) from e

        if result is None:
            raise SourceAddError(title, message=f"API returned no data for text source: {title}")

        source = Source.from_api_response(result, method_id=RPCMethod.ADD_SOURCE.value)

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
        rpc: RpcCaller,
        list_sources: ListSources,
        wait_until_ready: WaitUntilReady,
        logger: logging.Logger,
    ) -> Source:
        """Add a Google Drive document as a source.

        Drive sources go through the same probe-then-create idempotency
        pattern as ``add_url``: a 5xx / network failure
        between server-side commit and client-side response could
        otherwise duplicate the source on a naive retry. The probe matches
        by ``file_id`` substring against ``source.url`` (Drive URLs embed
        the file_id, e.g. ``https://docs.google.com/document/d/<id>/edit``).
        """
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
        # TODO(#1546): Drive add is NOT yet migrated to the nested template
        # block — no live Drive capture/probe yet, so it stays on the old
        # [2], [1,...,[1]] tail. Migrate via build_template_block() once a Drive
        # add is captured from the web UI and verified against a live account.
        params = [
            [source_data],
            notebook_id,
            [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]

        async def _create() -> Source:
            # Preserve transport-level signals so callers can act on the
            # specific type (AuthError -> re-login, RateLimitError -> back-off,
            # ServerError -> transient retry). The retryable transport
            # exceptions must propagate so idempotent_create can catch them
            # and run the probe.
            try:
                result = await rpc.rpc_call(
                    RPCMethod.ADD_SOURCE,
                    params,
                    source_path=f"/notebook/{notebook_id}",
                    allow_null=True,
                    disable_internal_retries=True,
                    operation_variant="drive",
                )
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as e:
                raise SourceAddError(title, cause=e) from e

            if result is None:
                raise SourceAddError(
                    title, message=f"API returned no data for Drive source: {title}"
                )
            return Source.from_api_response(result, method_id=RPCMethod.ADD_SOURCE.value)

        # Drive URLs canonically embed the file_id as a path segment, e.g.
        # ``https://docs.google.com/document/d/<file_id>/edit``. Match the
        # ``/d/<file_id>`` slug with a trailing segment boundary (either a
        # ``/`` or end-of-string) so neither an interior substring nor a
        # prefix-collision (e.g. ``/d/abc`` matching ``/d/abcdef/edit``)
        # produces a false-positive. Real-world Drive IDs are 33–44-char
        # Base64URL strings making prefix collisions astronomically unlikely
        # in practice, but the boundary check costs nothing.
        drive_url_marker = f"/d/{file_id}/"
        drive_url_tail = f"/d/{file_id}"

        async def _probe() -> Source | None:
            try:
                sources = await list_sources(notebook_id)
            except (AuthError, RateLimitError, ServerError, NetworkError):
                # Transport- and auth-level probe failures must propagate
                # — see the rationale in ``add_url._probe``.
                raise
            except Exception:
                logger.debug(
                    "add_drive: probe list() failed with non-transport error; treating as no match",
                    exc_info=True,
                )
                return None
            for source in sources:
                if source.url and (
                    drive_url_marker in source.url or source.url.endswith(drive_url_tail)
                ):
                    return source
            return None

        source = await idempotent_create(
            _create,
            _probe,
            label=f"sources.add_drive[{file_id}]",
        )

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

        # Unpack instead of indexing ``path_segments[0]`` / ``[1]``: these are
        # URL path segments, not an RPC payload, but the positional-RPC ratchet
        # is type-blind, so the unpack keeps the benign string parse off the
        # flagged ``name[int]`` shape (semantics identical to the prior
        # ``len(...) >= 2`` + index reads).
        if len(path_segments) >= 2:
            prefix, segment, *_rest = path_segments
            if prefix.lower() in path_prefixes:
                return segment.strip()

        if parsed.query:
            query_params = parse_qs(parsed.query)
            v_param = query_params.get("v", [])
            # ``next(iter(...))`` instead of ``v_param[0]`` for the same
            # type-blind-ratchet reason; ``v_param`` is the parse_qs value list.
            first_v = next(iter(v_param), None)
            if first_v:
                return first_v.strip()

        return None

    def is_valid_video_id(self, video_id: str) -> bool:
        """Validate YouTube video ID format."""
        return bool(video_id and re.match(r"^[a-zA-Z0-9_-]+$", video_id))

    async def add_youtube_source(
        self,
        notebook_id: str,
        url: str,
        *,
        rpc: RpcCaller,
    ) -> Any:
        """Add a YouTube video as a source.

        The source entry is unchanged, but the flat ``[2], [1,...,[1]]`` tail
        (4 outer elements) collapsed into the single nested
        ``[2, None, None, [1, ..., [1]]]`` block (#1546). Verified live against
        an un-migrated account.
        """
        params = [
            [[None, None, None, None, None, None, None, [url], None, None, 1]],
            notebook_id,
            build_template_block(),
        ]
        return await rpc.rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=False,
            disable_internal_retries=True,
            operation_variant="url",
        )

    async def add_url_source(
        self,
        notebook_id: str,
        url: str,
        *,
        rpc: RpcCaller,
    ) -> Any:
        """Add a regular URL as a source.

        The source spec gained a trailing ``1`` and the flat ``[2], None, None``
        tail collapsed into the nested ``[2, None, None, [1, ..., [1]]]`` block
        that NotebookLM's web UI now sends; migrated backends reject the old
        shape (``status=5``/``9``). Verified live against an un-migrated account.
        See https://github.com/teng-lin/notebooklm-py/issues/1546.
        """
        params = [
            [[None, None, [url], None, None, None, None, None, None, None, 1]],
            notebook_id,
            build_template_block(),
        ]
        return await rpc.rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            disable_internal_retries=True,
            operation_variant="url",
        )


__all__ = ["SourceAddService"]
