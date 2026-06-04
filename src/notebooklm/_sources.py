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

from ._deprecation import future_errors_enabled
from ._lookup import resolve_get
from ._runtime.config import DEFAULT_MAX_CONCURRENT_UPLOADS
from ._runtime.contracts import RpcCaller
from ._settings import build_get_user_settings_params, extract_account_limits
from ._source import upload as _source_upload
from ._source.add import SourceAddService
from ._source.content import SourceContentRenderer
from ._source.listing import SourceLister
from ._source.polling import SourcePoller
from ._source.upload import SourceUploadPipeline
from ._source.upload_payloads import build_rename_source_params
from ._types.research import SourceGuide
from ._url_utils import is_youtube_url
from .exceptions import SourceNotFoundError
from .rpc import RPCMethod
from .types import (
    Source,
    SourceFulltext,
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
        async with NotebookLMClient.from_storage() as client:
            sources = await client.sources.list(notebook_id)
            new_src = await client.sources.add_url(notebook_id, "https://example.com")
            await client.sources.rename(notebook_id, new_src.id, "Better Title")
    """

    def __init__(
        self,
        rpc: RpcCaller,
        *,
        uploader: SourceUploadPipeline,
        upload_timeout: httpx.Timeout | None = None,
        max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
    ):
        """Initialize the sources API.

        Args:
            rpc: The narrow :class:`RpcCaller` capability — sources
                only needs ``rpc_call(...)`` for its own RPC paths
                (delete, rename, refresh, freshness, drive add, text add).
                Upload-flow capabilities (``kernel``, ``auth``,
                ``operation_scope``) are owned by ``uploader``.
            uploader: Stateful file-upload pipeline. REQUIRED — wired explicitly
                by :class:`NotebookLMClient` (the only composition root that
                knows the concrete ``Kernel`` + ``AuthMetadata`` +
                ``record_upload_queue_wait`` callback). Direct callers must
                supply a :class:`SourceUploadPipeline` instance themselves;
                there is no implicit fallback.
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
            max_concurrent_uploads: Ceiling for concurrent
                :meth:`add_file` uploads. The semaphore is owned by this
                Sources upload pipeline, not by the shared core/session.
        """
        # ``upload_timeout`` and ``max_concurrent_uploads`` are accepted for
        # API stability — the actual upload pipeline that honors them is
        # constructed by the :class:`NotebookLMClient` composition root and
        # injected via ``uploader=``. They are stored here only as historical
        # attributes for callers that introspect the instance.
        self._rpc = rpc
        self._adder = SourceAddService()
        self._content = SourceContentRenderer(self._rpc, logger=logger)
        self._lister = SourceLister(self._rpc)
        self._poller = SourcePoller()
        self._upload_timeout = upload_timeout
        self._max_concurrent_uploads = max_concurrent_uploads
        self._uploader = uploader
        self._uploader.configure_source_limit_lookup(self._get_source_limit)
        # Single owner for the source-lifecycle verbs: the upload pipeline
        # delegates its ``list_sources`` / ``get_source`` / ``wait_*`` verbs
        # to the SAME ``SourceLister`` / ``SourcePoller`` instances this API
        # uses, rather than re-constructing parallel copies (issue #1205).
        self._uploader.configure_source_lifecycle(
            lister=self._lister,
            poller=self._poller,
        )

    async def _rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any:
        """Delegate through the current core RPC method for late-bound test overrides."""
        return await self._rpc.rpc_call(
            method,
            params,
            source_path=source_path,
            allow_null=allow_null,
            _is_retry=_is_retry,
            disable_internal_retries=disable_internal_retries,
            operation_variant=operation_variant,
        )

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

        .. deprecated:: 0.7.0
            Returning ``None`` for a missing source is deprecated and emits a
            :class:`DeprecationWarning`. In **v0.8.0** this method will raise
            :class:`~notebooklm.exceptions.SourceNotFoundError` instead, to match
            ``notebooks.get`` (issue #1247); wrap in ``try/except
            SourceNotFoundError`` to keep handling missing sources. Suppress with
            ``NOTEBOOKLM_QUIET_DEPRECATIONS``, or set ``NOTEBOOKLM_FUTURE_ERRORS=1``
            to preview the v0.8.0 raise now.
        """
        # ``resolve_get`` single-sources the warn-vs-raise decision (#1247);
        # internal callers needing the silent lookup use ``get_or_none``.
        return resolve_get(
            await self.get_or_none(notebook_id, source_id),
            not_found=SourceNotFoundError(source_id),
            resource="source",
        )

    async def get_or_none(self, notebook_id: str, source_id: str) -> Source | None:
        """Get a source by ID, returning ``None`` when it does not exist.

        The sanctioned ``None``-on-miss lookup (ADR-0019): unlike :meth:`get`
        — which is slated to raise :class:`~notebooklm.exceptions.SourceNotFoundError`
        on a miss in v0.8.0 (issue #1247) — this returns ``None`` for a genuine
        absence and emits no deprecation warning. Transport, auth, and decode
        faults are **not** swallowed; only a real "not found" yields ``None``.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID.

        Returns:
            The :class:`~notebooklm.types.Source`, or ``None`` if not found.
        """
        return await self._lister.get(
            notebook_id,
            source_id,
            list_sources=self.list,
        )

    # Internal optional-lookup alias: the readiness pollers and CLI service
    # layer probe for a source without tripping the public ``get`` deprecation
    # warning. Kept as a stable private name so those call sites need no churn.
    _get_or_none = get_or_none

    async def wait_until_ready(
        self,
        notebook_id: str,
        source_id: str,
        timeout: float = 120.0,
        initial_interval: float = 1.0,
        max_interval: float = 10.0,
        backoff_factor: float = 1.5,
        transient_error_types: tuple[int | None, ...] | None = None,
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
            transient_error_types=transient_error_types,
            get_source=self._get_or_none,
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
        transient_error_types: tuple[int | None, ...] | None = None,
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
            transient_error_types=transient_error_types,
            get_source=self._get_or_none,
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
        *,
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
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        idempotent: bool = False,
    ) -> Source:
        """Add a text source (copied text) to a notebook.

        Args:
            notebook_id: The notebook ID.
            title: Title for the source.
            content: Text content.
            wait: If True, wait for source to be ready before returning.
            wait_timeout: Maximum seconds to wait if wait=True (default: 120).
            idempotent: Opt-in safety flag that REFUSES the call rather
                than risk silent duplication on retry. Text sources
                lack a reliable server-side dedupe key (titles non-unique;
                content not exposed in the source list), so the
                probe-then-retry pattern used by ``add_url`` cannot be
                applied here. When True, raises
                :class:`NonIdempotentRetryError` immediately. Default
                ``False`` no longer relies on the inner transport retry
                loop — as of the variant-keyed idempotency rollout, the
                ``(ADD_SOURCE, "text")`` registry entry classifies this
                call as ``NON_IDEMPOTENT_NO_RETRY``, which force-disables
                the inner 5xx / 429 / network retry loop so the first
                failure surfaces immediately instead of risking a
                duplicate on retry. For idempotent text imports, embed a
                UUID in the title and dedupe client-side. See
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
            rpc=self._rpc,
            wait_until_ready=self.wait_until_ready,
            logger=logger,
        )

    async def add_file(
        self,
        notebook_id: str,
        file_path: str | Path,
        mime_type: str | None = None,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
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

        Concurrency / FD lifecycle:
            ``add_file`` enters a drain-tracked ``operation_scope("upload:0")``
            before waiting on the Sources-owned semaphore, so graceful
            shutdown tracks both active and semaphore-queued uploads.
            The upload section runs under the Sources-owned upload
            pipeline semaphore, which bounds simultaneous in-flight
            uploads at ``max_concurrent_uploads`` (default 4).
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
            mime_type: Optional content type for the upload start handshake.
                When omitted, the MIME type is inferred from the filename
                extension. Supplying a value overrides inference.
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

        Raises:
            ValidationError: If the path is not a regular file, the title is
                empty, or the upload is an HTML-family file that NotebookLM's
                upload endpoint rejects. Convert saved web pages to text,
                Markdown, or PDF before calling this method.
        """
        return await self._uploader.add_file(
            notebook_id,
            file_path,
            mime_type=mime_type,
            wait=wait,
            wait_timeout=wait_timeout,
            title=title,
            on_progress=on_progress,
        )

    async def add_drive(
        self,
        notebook_id: str,
        file_id: str,
        title: str,
        mime_type: str = "application/vnd.google-apps.document",
        *,
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
            rpc=self._rpc,
            list_sources=self.list,
            wait_until_ready=self.wait_until_ready,
            logger=logger,
        )

    async def delete(self, notebook_id: str, source_id: str) -> None:
        """Delete a source from a notebook.

        Idempotent: deleting an already-absent source succeeds (returns
        ``None``) and never raises ``SourceNotFoundError``. Real failures
        (``403``/``5xx``/auth/transport) still propagate.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to delete.

        .. versionchanged:: 0.7.0
            **Breaking change:** previously returned a hardcoded ``True``;
            now returns ``None`` (issue #1211). ``if await source.delete(...):``
            no longer enters its block.
        """
        logger.debug("Deleting source %s from notebook %s", source_id, notebook_id)
        params = [[[source_id]]]
        await self._rpc.rpc_call(
            RPCMethod.DELETE_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def rename(
        self,
        notebook_id: str,
        source_id: str,
        new_title: str,
        *,
        return_object: bool = True,
    ) -> Source | None:
        """Rename a source.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to rename.
            new_title: The new title.
            return_object: When ``True`` (default), return the renamed
                :class:`~notebooklm.types.Source` (preferring the
                ``UPDATE_SOURCE`` echo, fetching only on a null echo). When
                ``False``, return ``None`` without hydrating. Under the v0.8.0
                preview ``False`` still returns ``None`` but adds miss-detection
                (the flag gates detection, not return — see ``Raises``).

        Returns:
            The renamed :class:`~notebooklm.types.Source`, or ``None`` when
            ``return_object=False``.

        Raises:
            SourceNotFoundError: if the source does not exist (detected via a
                content/list fetch, not a 404). Always when ``return_object=True``;
                also on ``False`` under ``NOTEBOOKLM_FUTURE_ERRORS``.

        .. versionchanged:: 0.7.0
            **Breaking change:** no longer fabricates an unverified
            ``Source(id, title)`` on a null echo; it hydrates and raises
            :class:`SourceNotFoundError` (#1255), plus ``return_object``.
        """
        logger.debug("Renaming source %s to: %s", source_id, new_title)
        params = build_rename_source_params(source_id, new_title)
        result = await self._rpc.rpc_call(
            RPCMethod.UPDATE_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        if result and return_object:
            return Source.from_api_response(result, method_id=RPCMethod.UPDATE_SOURCE.value)
        # Null echo: hydrate via the internal lookup (never public ``get()`` —
        # #1247) so a miss raises. ``False`` skips it when existence is proven
        # (echo) or flag-off; the v0.8.0 preview (#1362) runs it to detect a miss.
        if not return_object and (result or not future_errors_enabled()):
            return None
        source = await self._get_or_none(notebook_id, source_id)
        if source is None:
            raise SourceNotFoundError(source_id, method_id=RPCMethod.UPDATE_SOURCE.value)
        return None if not return_object else source

    async def refresh(self, notebook_id: str, source_id: str) -> bool:
        """Refresh a source to get updated content (for URL/Drive sources).

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to refresh.

        Returns:
            ``True`` if refresh was initiated (failures raise first, so it is
            uninformative). Under ``NOTEBOOKLM_FUTURE_ERRORS`` (#1290) returns
            ``None``; ``-> bool`` stays until v0.8.0.
        """
        params = [None, [source_id], [2]]
        await self._rpc.rpc_call(
            RPCMethod.REFRESH_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        # v0.8.0 preview (#1290): uninformative ``True`` -> ``None`` (runtime-only).
        if future_errors_enabled():
            return None  # type: ignore[return-value]
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
        result = await self._rpc.rpc_call(
            RPCMethod.CHECK_SOURCE_FRESHNESS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        # Shapes by source type: ``[]`` or ``[[null, true, [id]]]`` = fresh
        # (URL / Drive); bare ``True`` = fresh; bare ``False`` = stale.
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

    async def get_guide(self, notebook_id: str, source_id: str) -> SourceGuide:
        """Get AI-generated summary and keywords for a specific source.

        This is the "Source Guide" feature shown when clicking on a source
        in the NotebookLM UI.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to get guide for.

        Returns:
            A :class:`~notebooklm._types.research.SourceGuide` with:
                - ``summary``: AI-generated summary with **bold** keywords (markdown)
                - ``keywords``: tuple of topic keyword strings (``guide["keywords"]``
                  still yields a ``list`` for back-compat)

            Use attribute access (``guide.summary``). Legacy
            ``guide["summary"]`` dict-subscript access still works (with a
            ``DeprecationWarning``) until v0.8.0.
        """
        return await self._content.get_guide(notebook_id, source_id)

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
            SourceFulltext object with content, title, kind, url, and char_count.

        Raises:
            SourceNotFoundError: If the source is not found or returns no data.

        Note:
            Source type codes: 1=google_docs, 2=google_other, 3=pdf, 4=pasted_text,
            5=web_page, 8=generated_text, 9=youtube

            The ``"markdown"`` format works by requesting the HTML rendition
            from the API (params ``[3],[3]`` instead of ``[2],[2]``) and
            converting it via *markdownify*.
        """
        return await self._content.get_fulltext(
            notebook_id,
            source_id,
            output_format=output_format,
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
        return self._content.extract_all_text(data, max_depth=max_depth)

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

        ``disable_internal_retries=True``: ADD_SOURCE is a
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
            rpc=self._rpc,
        )

    async def _add_url_source(self, notebook_id: str, url: str) -> Any:
        """Add a regular URL as a source.

        ``disable_internal_retries=True``: see
        ``_add_youtube_source`` for the rationale.
        """
        return await self._adder.add_url_source(
            notebook_id,
            url,
            rpc=self._rpc,
        )

    async def _register_file_source(self, notebook_id: str, filename: str) -> str:
        """Register a file source intent and get SOURCE_ID."""
        return await self._uploader.register_file_source(
            notebook_id,
            filename,
        )

    async def _get_source_limit(self) -> int | None:
        """Return the current account's per-notebook source limit when advertised."""
        result = await self._rpc_call(
            RPCMethod.GET_USER_SETTINGS,
            build_get_user_settings_params(),
            source_path="/",
        )
        return extract_account_limits(result).source_limit

    async def _start_resumable_upload(
        self,
        notebook_id: str,
        filename: str,
        file_size: int,
        source_id: str,
        content_type: str,
    ) -> str:
        """Start a resumable upload session and get the upload URL."""
        return await self._uploader.start_resumable_upload(
            notebook_id,
            filename,
            file_size,
            source_id,
            content_type,
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

        File-descriptor contract:
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
          dangling-session guarantee.

          A legacy ``Path`` argument is still accepted; the helper opens
          + closes the FD itself in that branch. ``add_file`` never
          takes that path — the Path branch exists only for the
          existing direct-call unit tests in
          ``tests/unit/test_sources_upload.py``.

        Cancellation contract:
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
            logger=logger,
        )
