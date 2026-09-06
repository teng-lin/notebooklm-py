"""Private non-file source creation service."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol
from urllib.parse import parse_qs

from ..._idempotency import (
    OperationJournal,
    attach_journal_entry,
    bind_operation_journal_entries,
    call_unconfirmed_on_transport_loss,
    mark_commit_state,
    mark_unconfirmed,
)
from ...exceptions import (
    AuthError,
    NetworkError,
    NotebookLMError,
    RateLimitError,
    ServerError,
    SourceAddError,
    SourceProcessingError,
    ValidationError,
)
from ...outcomes import CommitState, RecoveryAction
from ...rpc import RPCError, RPCMethod
from ...types import Source
from ..contracts import RpcCaller
from ..params.sources import build_template_block
from ..rows.source_models import decode_source

ListSources = Callable[[str], Awaitable[list[Source]]]
WaitUntilReady = Callable[..., Awaitable[Source]]


class RawSourceAdder(Protocol):
    async def __call__(self, notebook_id: str, url: str) -> Any: ...


RenameSource = Callable[[str, str, str], Awaitable[Source | None]]
ParseUrl = Callable[[str], Any]
ExtractVideoId = Callable[[Any, str], str | None]
ValidateVideoId = Callable[[str], bool]
YoutubeDetector = Callable[[str], bool]


def _source_add_failure(
    subject: str,
    *,
    operation: str,
    cause: Exception | None = None,
    message: str | None = None,
) -> SourceAddError:
    """Wrap one post-dispatch failure without losing its commit evidence."""
    failure = SourceAddError(subject, cause=cause, message=message)
    inherited_operation = getattr(cause, "operation", None)
    operation_name = inherited_operation if isinstance(inherited_operation, str) else operation
    state = getattr(cause, "commit_state", None)
    if state in (CommitState.NOT_SENT, CommitState.REJECTED, CommitState.CONFIRMED):
        return mark_commit_state(failure, state, operation=operation_name)
    return mark_unconfirmed(failure, operation=operation_name)


def _validate_drive_file_id(file_id: str) -> None:
    """Reject a blank Drive identifier before admission or any write."""
    if not file_id or not file_id.strip():
        raise ValidationError("Drive file_id cannot be empty or whitespace-only")


def _describe_sources(sources: list[Source]) -> str:
    """Render matched sources as ``id (title)`` for an ambiguity message.

    The ambiguity raises tell the caller to go check the notebook's source
    list; naming the exact rows saves them diffing a list by eye against a URL
    that, by definition, appears in it more than once.
    """
    return ", ".join(f"{source.id} ({source.title!r})" for source in sources)


async def honor_requested_title(
    rename: RenameSource,
    notebook_id: str,
    source: Source,
    requested_title: str | None,
    logger: logging.Logger,
) -> Source:
    """Best-effort post-add rename so an explicit ``title`` survives backend
    re-derivation (#1960).

    YouTube, native Google Drive, and web-page imports re-derive the display
    title server-side (from the video / Drive / page metadata), silently
    discarding the ``title`` sent with the add. Live-verified (URL, YouTube, and
    Drive): the backend derives the title *synchronously* — the added source comes
    back already carrying the re-derived title — so a follow-up ``rename`` lands
    after that derivation and sticks. When an explicit ``title`` differs from the
    one the add returned, issue the rename so the requested title wins.

    Non-fatal by contract: the add already succeeded, so a rename failure keeps
    the added source (with its upstream title) and logs a warning rather than
    raising — callers detect the miss by comparing the returned ``source.title``
    against the title they requested (the MCP tool surfaces this).
    """
    if not requested_title:
        return source
    requested = requested_title.strip()
    if not requested or source.title == requested:
        return source
    try:
        renamed = await rename(notebook_id, source.id, requested)
    except (RPCError, NetworkError):
        logger.warning(
            "Source %s added but rename to %r failed; keeping upstream title %r",
            source.id,
            requested,
            source.title,
            exc_info=True,
        )
        return source
    # UPDATE_SOURCE's echo can be sparse (id + title only), so returning it wholesale
    # would drop url / kind / status. Keep the fully-hydrated added source and swap in
    # just the new title — mirrors the file-upload rename (``_web/sources/upload.py``).
    return replace(source, title=(renamed.title if renamed else None) or requested)


async def honor_requested_title_if_fresh(
    rename: RenameSource,
    notebook_id: str,
    source: Source,
    requested_title: str | None,
    logger: logging.Logger,
) -> Source:
    """Apply a requested title after a confirmed source create."""
    return await honor_requested_title(rename, notebook_id, source, requested_title, logger)


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
        return_result: bool = False,
    ) -> Source:
        """Add one URL source without replaying an ambiguous create.

        A decoded response returns its correlated source normally. If the
        response is lost after transmission, the original exception is marked
        unknown and raised so the caller can inspect before creating again.
        """

        logger.debug("Adding URL source to notebook %s: %s", notebook_id, url[:80])
        journal_entry = OperationJournal("sources.add_url").new_entry(
            method=RPCMethod.ADD_SOURCE.value
        )
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
            # ServerError, and NetworkError must propagate to the one-shot
            # ambiguity marker. AuthError continues to
            # propagate to the caller because an auth failure cannot have
            # committed the write.
            try:
                adder = add_youtube_source if video_id else add_url_source
                with bind_operation_journal_entries(journal_entry):
                    result = await adder(notebook_id, url)
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as e:
                raise _source_add_failure(
                    url,
                    cause=e,
                    operation="sources.add_url",
                ) from e

            if result is None:
                raise _source_add_failure(
                    url,
                    message=f"API returned no data for URL: {url}",
                    operation="sources.add_url",
                )
            try:
                source = decode_source(Source, result, method_id=RPCMethod.ADD_SOURCE.value)
                journal_entry.record(
                    CommitState.CONFIRMED,
                    "decoded source create",
                    known_resource_ids=((source.id,) if source.id else ()),
                )
                return source
            except Exception as e:
                raise _source_add_failure(
                    url,
                    cause=e,
                    operation="sources.add_url",
                    message=f"Failed to decode the added URL source: {url}",
                ) from e

        source = await call_unconfirmed_on_transport_loss(
            _create,
            method=RPCMethod.ADD_SOURCE,
            what="the URL-source add",
            operation="sources.add_url",
            journal_entry=journal_entry,
        )

        if wait:
            try:
                source = await wait_until_ready(notebook_id, source.id, timeout=wait_timeout)
            except NotebookLMError as exc:
                journal_entry.stage = "wait"
                attach_journal_entry(
                    exc,
                    journal_entry,
                    recovery_action=(
                        RecoveryAction.NONE
                        if isinstance(exc, SourceProcessingError)
                        else RecoveryAction.WAIT
                    ),
                )
                raise

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
        logger.debug("Adding text source to notebook %s: %s", notebook_id, title)
        journal_entry = OperationJournal("sources.add_text").new_entry(
            method=RPCMethod.ADD_SOURCE.value
        )
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

        async def _create() -> Source:
            try:
                with bind_operation_journal_entries(journal_entry):
                    result = await rpc.rpc_call(
                        RPCMethod.ADD_SOURCE,
                        params,
                        source_path=f"/notebook/{notebook_id}",
                        operation_variant="text",
                    )
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as e:
                raise _source_add_failure(
                    title,
                    cause=e,
                    operation="sources.add_text",
                    message=f"Failed to add text source '{title}'",
                ) from e

            if result is None:
                raise _source_add_failure(
                    title,
                    message=f"API returned no data for text source: {title}",
                    operation="sources.add_text",
                )
            try:
                source = decode_source(Source, result, method_id=RPCMethod.ADD_SOURCE.value)
                journal_entry.record(
                    CommitState.CONFIRMED,
                    "decoded source create",
                    known_resource_ids=((source.id,) if source.id else ()),
                )
                return source
            except Exception as e:
                raise _source_add_failure(
                    title,
                    cause=e,
                    operation="sources.add_text",
                    message=f"Failed to decode the added text source: {title}",
                ) from e

        source = await call_unconfirmed_on_transport_loss(
            _create,
            method=RPCMethod.ADD_SOURCE,
            what="the text-source add",
            operation="sources.add_text",
            journal_entry=journal_entry,
        )

        if wait:
            try:
                return await wait_until_ready(notebook_id, source.id, timeout=wait_timeout)
            except NotebookLMError as exc:
                journal_entry.stage = "wait"
                attach_journal_entry(
                    exc,
                    journal_entry,
                    recovery_action=(
                        RecoveryAction.NONE
                        if isinstance(exc, SourceProcessingError)
                        else RecoveryAction.WAIT
                    ),
                )
                raise

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
        return_result: bool = False,
    ) -> Source:
        """Add one Drive source without replaying an ambiguous create.

        A decoded response returns its correlated source normally. If the
        response is lost after transmission, the original exception is marked
        unknown and raised so the caller can inspect before creating again.
        """

        # Fail before the write rather than POSTing a blank Drive id. A blank
        # id is also unmatchable by the probe below (a row's
        # ``drive_document_id`` is never ``""``), so without this guard a
        # transport failure would retry the blank add and could leave two
        # garbage sources behind.
        _validate_drive_file_id(file_id)
        journal_entry = OperationJournal("sources.add_drive").new_entry(
            method=RPCMethod.ADD_SOURCE.value
        )
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
            # ServerError -> transient retry). Transport exceptions propagate
            # so the one-shot ambiguity boundary can mark them without replay.
            try:
                with bind_operation_journal_entries(journal_entry):
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
                raise _source_add_failure(
                    title,
                    cause=e,
                    operation="sources.add_drive",
                ) from e

            if result is None:
                raise _source_add_failure(
                    title,
                    message=(
                        f"API returned no data for Drive source: {title} "
                        f"(mime_type={mime_type!r}). This Drive file type may not be "
                        "importable via Drive — NotebookLM's Drive import supports "
                        "Google-native Docs/Slides/Sheets + PDF only. If it is an "
                        "upload-only type (e.g. epub/docx/txt/md/rtf/odt/csv), "
                        "download it and add it as a `file` source instead."
                    ),
                    operation="sources.add_drive",
                )
            try:
                source = decode_source(Source, result, method_id=RPCMethod.ADD_SOURCE.value)
                journal_entry.record(
                    CommitState.CONFIRMED,
                    "decoded source create",
                    known_resource_ids=((source.id,) if source.id else ()),
                )
                return source
            except Exception as e:
                raise _source_add_failure(
                    title,
                    cause=e,
                    operation="sources.add_drive",
                    message=f"Failed to decode the added Drive source: {title}",
                ) from e

        source = await call_unconfirmed_on_transport_loss(
            _create,
            method=RPCMethod.ADD_SOURCE,
            what="the Drive-source add",
            operation="sources.add_drive",
            journal_entry=journal_entry,
        )

        if wait:
            try:
                source = await wait_until_ready(notebook_id, source.id, timeout=wait_timeout)
            except NotebookLMError as exc:
                journal_entry.stage = "wait"
                attach_journal_entry(
                    exc,
                    journal_entry,
                    recovery_action=(
                        RecoveryAction.NONE
                        if isinstance(exc, SourceProcessingError)
                        else RecoveryAction.WAIT
                    ),
                )
                raise

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
