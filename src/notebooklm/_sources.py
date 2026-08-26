"""Source operations API."""

import asyncio
import builtins
import logging
import time
from collections.abc import Callable, Collection
from pathlib import Path
from typing import IO, Any, Final, Literal, cast

import httpx

from ._backend import BackendAdapter, BackendError
from ._deadline import RuntimeDeadlineFactory
from ._lookup import unwrap_or_raise
from ._runtime.config import DEFAULT_MAX_CONCURRENT_UPLOADS
from ._semantic.compat import (
    project_backend_call,
    project_backend_error,
    project_source_add_failure,
)
from ._semantic.projectors import (
    project_source,
    project_source_fulltext,
    project_source_guide,
    record_source,
)
from ._semantic.services.read import SourceReadService
from ._semantic.services.source import SourceService
from ._source import upload as _source_upload
from ._source.batch import SourceUrlBatchItem
from ._source.content import SourceContentRenderer
from ._source.listing import _snapshot_enum_filter
from ._source.polling import SourcePoller
from ._source.upload import SourceUploadPipeline
from ._types.research import SourceGuide
from ._url_utils import (
    extract_youtube_video_id,
    is_valid_youtube_video_id,
    youtube_video_id_from_parsed_url,
)
from .exceptions import (
    SourceAddError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
)
from .rpc.types import source_status_to_str
from .types import (
    Source,
    SourceFulltext,
    SourceStatus,
    SourceType,
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
        rpc: Any,
        *,
        uploader: SourceUploadPipeline,
        upload_timeout: httpx.Timeout | None = None,
        max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
        deadline_factory: RuntimeDeadlineFactory | None = None,
        _backend: BackendAdapter | None = None,
    ):
        """Initialize the sources API.

        Args:
            rpc: Legacy compatibility handle retained for callers that introspect
                the facade. Source workflows dispatch through ``_backend``.
                Upload-flow capabilities (``kernel``, ``auth``, and
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
            deadline_factory: Private composition dependency used to mint one
                aggregate deadline for service-owned Source workflows.
            _backend: Private semantic backend supplied by the client composition root.
        """
        # ``upload_timeout`` / ``max_concurrent_uploads`` are accepted for API
        # stability but honored by the injected ``uploader=`` pipeline (built by
        # the :class:`NotebookLMClient` composition root); stored here only as
        # historical attributes for callers that introspect the instance.
        self._rpc = rpc
        self._read_service = SourceReadService(_backend) if _backend is not None else None
        self._source_service = (
            SourceService(_backend, deadline_factory=deadline_factory)
            if _backend is not None
            else None
        )
        self._content = SourceContentRenderer(None, logger=logger)
        self._upload_timeout = upload_timeout
        self._max_concurrent_uploads = max_concurrent_uploads
        self._uploader = uploader

    def _require_read_service(self) -> SourceReadService:
        """Return the composition-root service for the migrated read slice."""
        if self._read_service is None:
            raise RuntimeError("SourcesAPI semantic read backend was not configured")
        return self._read_service

    def _require_source_service(self) -> SourceService:
        """Return the composition-root service for the migrated Source slice."""
        if self._source_service is None:
            raise RuntimeError("SourcesAPI semantic source backend was not configured")
        return self._source_service

    @staticmethod
    def _compat_read_error(error: BackendError) -> Exception:
        """Project one neutral backend error at the public compatibility boundary."""
        return project_backend_error(error)

    async def list(
        self,
        notebook_id: str,
        *,
        strict: bool = False,
        statuses: Collection[SourceStatus] | None = None,
        types: Collection[SourceType] | None = None,
    ) -> list[Source]:
        """List all sources in a notebook.

        Args:
            notebook_id: The notebook ID.
            strict: Reject malformed source rows and conflicting duplicate IDs.
                Malformed response envelopes always raise ``RPCError``. Use
                ``strict=True`` when ``len(result)`` must be an exact count of
                uniquely addressable matching sources.
            statuses: Optional collection of accepted statuses. Members are ORed.
            types: Optional collection of accepted source types. Members are ORed.
                When both filters are supplied, a source must match both.

        Returns:
            Source objects in backend order after normalization and filtering.
        """
        status_filter = _snapshot_enum_filter(
            statuses,
            enum_type=SourceStatus,
            parameter="statuses",
        )
        type_filter = _snapshot_enum_filter(
            types,
            enum_type=SourceType,
            parameter="types",
        )
        public_error: Exception | None = None
        try:
            records = await self._require_read_service().list(
                notebook_id,
                strict=strict,
                statuses=(
                    None
                    if status_filter is None
                    else frozenset(source_status_to_str(status) for status in status_filter)
                ),
                kinds=(
                    None
                    if type_filter is None
                    else frozenset(source_type.value for source_type in type_filter)
                ),
            )
        except BackendError as error:
            public_error = self._compat_read_error(error)
        else:
            return [project_source(record) for record in records]
        assert public_error is not None
        raise public_error

    async def get(self, notebook_id: str, source_id: str) -> Source:
        """Get details of a specific source.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID.

        Returns:
            The :class:`~notebooklm.types.Source` with its current status.

        Raises:
            SourceNotFoundError: If no source with ``source_id`` exists (matches
                ``notebooks.get``; issue #1247). Use :meth:`get_or_none` for the
                sanctioned ``None``-on-miss lookup.
        """
        # ``unwrap_or_raise`` single-sources the raise-on-miss decision (#1247);
        # internal callers needing the silent lookup use ``get_or_none``.
        return unwrap_or_raise(
            await self.get_or_none(notebook_id, source_id),
            SourceNotFoundError(source_id),
        )

    async def get_or_none(self, notebook_id: str, source_id: str) -> Source | None:
        """Get a source by ID, returning ``None`` when it does not exist.

        The sanctioned ``None``-on-miss lookup (ADR-0019): unlike :meth:`get`
        — which now raises :class:`~notebooklm.exceptions.SourceNotFoundError`
        on a miss (#1247) — this returns ``None`` for a genuine absence and
        emits no deprecation warning. Transport, auth, and decode
        faults are **not** swallowed; only a real "not found" yields ``None``.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID.

        Returns:
            The :class:`~notebooklm.types.Source`, or ``None`` if not found.
        """
        list_method = self.list
        if getattr(list_method, "__func__", None) is not _ORIGINAL_SOURCES_LIST:
            sources = await list_method(notebook_id)
            return next((source for source in sources if source.id == source_id), None)

        public_error: Exception | None = None
        try:
            record = await self._require_read_service().get(notebook_id, source_id)
        except BackendError as error:
            public_error = self._compat_read_error(error)
        else:
            return None if record is None else project_source(record)
        assert public_error is not None
        raise public_error

    # Internal silent lookup for pollers/service code avoiding public ``get()`` misses.
    _get_or_none = get_or_none

    async def _wait_snapshot_sources(self, notebook_id: str) -> builtins.list[Source]:
        """Fetch one semantic snapshot for one facade-owned polling tick."""
        list_method = self.list
        if getattr(list_method, "__func__", None) is not _ORIGINAL_SOURCES_LIST:
            return await list_method(notebook_id)
        result = await project_backend_call(
            self._require_source_service().wait_snapshot(notebook_id)
        )
        return [project_source(record) for record in result.sources]

    async def _wait_snapshot_source(
        self,
        notebook_id: str,
        source_id: str,
    ) -> Source | None:
        get_method = self.get_or_none
        if getattr(get_method, "__func__", None) is not _ORIGINAL_SOURCES_GET_OR_NONE:
            return await get_method(notebook_id, source_id)
        sources = await self._wait_snapshot_sources(notebook_id)
        return next((source for source in sources if source.id == source_id), None)

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

        Polls until READY, terminal ERROR, or timeout. Configured transient
        source types (audio/media and unclassified by default) keep polling
        through status=ERROR because NotebookLM can report it briefly during
        transcription/classification.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to wait for.
            timeout: Maximum time to wait in seconds (default: 120).
            initial_interval: Initial polling interval in seconds (default: 1).
            max_interval: Maximum polling interval in seconds (default: 10).
            backoff_factor: Multiplier for polling interval (default: 1.5).
            transient_error_types: Source type codes whose status=ERROR is
                transient; ``None`` uses the default media/unclassified policy.

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
        return await SourcePoller().wait_until_ready(
            notebook_id,
            source_id,
            timeout=timeout,
            initial_interval=initial_interval,
            max_interval=max_interval,
            backoff_factor=backoff_factor,
            transient_error_types=transient_error_types,
            get_source=self._wait_snapshot_source,
            sleep=asyncio.sleep,
            monotonic=time.monotonic,
            logger=logger,
        )

    async def wait_all_until_ready(
        self,
        notebook_id: str,
        source_ids: builtins.list[str],
        timeout: float = 120.0,
        initial_interval: float = 1.0,
        max_interval: float = 10.0,
        backoff_factor: float = 1.5,
        transient_error_types: tuple[int | None, ...] | None = None,
    ) -> builtins.list[Source | SourceNotFoundError | SourceProcessingError | SourceTimeoutError]:
        """Wait for many sources with ONE notebook snapshot per poll tick.

        Returns one result per id, in input order; terminal per-source failures
        (:class:`SourceNotFoundError` / :class:`SourceProcessingError` /
        :class:`SourceTimeoutError`) are RETURNED, not raised. See
        :meth:`SourcePoller.wait_all_until_ready`.
        """
        return await SourcePoller().wait_all_until_ready(
            notebook_id,
            source_ids,
            timeout=timeout,
            initial_interval=initial_interval,
            max_interval=max_interval,
            backoff_factor=backoff_factor,
            transient_error_types=transient_error_types,
            list_sources=self._wait_snapshot_sources,
            sleep=asyncio.sleep,
            monotonic=time.monotonic,
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
        return await SourcePoller().wait_until_registered(
            notebook_id,
            source_id,
            timeout=timeout,
            initial_interval=initial_interval,
            max_interval=max_interval,
            backoff_factor=backoff_factor,
            transient_error_types=transient_error_types,
            get_source=self._wait_snapshot_source,
            sleep=asyncio.sleep,
            monotonic=time.monotonic,
            logger=logger,
        )

    async def wait_for_sources(
        self,
        notebook_id: str,
        source_ids: builtins.list[str],
        timeout: float = 120.0,
        **kwargs: Any,
    ) -> builtins.list[Source]:
        """Wait for multiple sources using one shared notebook snapshot per tick.

        Args:
            notebook_id: The notebook ID.
            source_ids: List of source IDs to wait for.
            timeout: Shared polling timeout in seconds.
            **kwargs: Polling options accepted by :meth:`wait_until_ready`.

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
        allowed_options = {
            "initial_interval",
            "max_interval",
            "backoff_factor",
            "transient_error_types",
        }
        unexpected = next((name for name in kwargs if name not in allowed_options), None)
        if unexpected is not None:
            raise TypeError(
                f"SourcesAPI.wait_for_sources() got an unexpected keyword argument {unexpected!r}"
            )

        outcomes = await SourcePoller().wait_all_until_ready(
            notebook_id,
            source_ids,
            timeout=timeout,
            initial_interval=kwargs.get("initial_interval", 1.0),
            max_interval=kwargs.get("max_interval", 10.0),
            backoff_factor=kwargs.get("backoff_factor", 1.5),
            transient_error_types=kwargs.get("transient_error_types"),
            fail_fast=True,
            list_sources=self._wait_snapshot_sources,
            sleep=asyncio.sleep,
            monotonic=time.monotonic,
            logger=logger,
        )
        return [cast(Source, outcome) for outcome in outcomes]

    async def add_url(
        self,
        notebook_id: str,
        url: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        title: str | None = None,
    ) -> Source:
        """Add a URL source to a notebook.

        Automatically detects YouTube URLs and uses the appropriate method.

        Fires one ``GET_NOTEBOOK`` before the create for the retry probe (#2204);
        that read bumps Recent position (#2126). See the service method's docs.

        Args:
            notebook_id: The notebook ID.
            url: The URL to add.
            wait: If True, wait for source to be ready before returning.
            wait_timeout: Maximum seconds to wait if wait=True (default: 120).
            title: Optional display title. YouTube/web-page imports re-derive it
                server-side; a supplied one is honored via best-effort :meth:`rename`
                (non-fatal; #1960).

        Returns:
            The created Source object. If wait=False, status may be PROCESSING.

        Example:
            source = await client.sources.add_url(nb_id, url, wait=True)
        """
        public_error: Exception | None = None
        try:
            result = await self._require_source_service().add_url(
                notebook_id,
                url,
                wait=wait,
                wait_timeout=wait_timeout,
                requested_title=title,
                # The public facade remains the sole readiness-polling authority.
                deadline=None,
            )
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            source = project_source(result.source)
            if wait:
                source = await self.wait_until_ready(
                    notebook_id,
                    source.id,
                    timeout=wait_timeout,
                )
                requested_title = title.strip() if title else ""
                if requested_title and source.title != requested_title:
                    finalized = await project_backend_call(
                        self._require_source_service().finalize_title(
                            notebook_id,
                            record_source(source),
                            requested_title,
                        )
                    )
                    return project_source(finalized.source)
                return source
            return source
        # Raise outside the BackendError catch frame so the reconstructed public
        # cause/context graph is not replaced by the private compatibility error.
        assert public_error is not None
        raise public_error

    async def _add_urls_batch(
        self,
        notebook_id: str,
        urls: builtins.list[str],
    ) -> builtins.list[SourceUrlBatchItem]:
        """Add validated URL entries with one batch-capable ``ADD_SOURCE`` RPC.

        Internal adapter seam for the existing MCP/REST batch endpoints.  The
        public single-item :meth:`add_url` contract remains unchanged; in
        particular, it retains precise probe-then-create recovery.  This bulk
        path never replays an uncertain write and returns typed positional
        outcomes after reconciling silently omitted failures.
        """
        public_error: Exception | None = None
        try:
            result = await self._require_source_service().add_urls_batch(
                notebook_id,
                tuple(urls),
            )
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return [
                SourceUrlBatchItem(
                    url=item.url,
                    source=(project_source(item.source) if item.source is not None else None),
                    error=(
                        cast(SourceAddError, project_source_add_failure(item.error))
                        if item.error is not None
                        else None
                    ),
                )
                for item in result.items
            ]
        raise public_error

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
        public_error: Exception | None = None
        try:
            result = await self._require_source_service().add_text(
                notebook_id,
                title,
                content,
                wait=wait,
                wait_timeout=wait_timeout,
                idempotent=idempotent,
                deadline=None,
            )
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            source = project_source(result.source)
            if wait:
                return await self.wait_until_ready(
                    notebook_id,
                    source.id,
                    timeout=wait_timeout,
                )
            return source
        raise public_error

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
        """Add a file source to a notebook using Google's resumable upload.

        Registers the source, opens an upload session, streams the file body (memory-efficient for
        large files), and — if a custom ``title`` is given — issues a follow-up ``UPDATE_SOURCE``
        rename (the file-add RPC has no title slot). Uploads run under the Sources-owned semaphore
        (``max_concurrent_uploads``, default 4), which also caps open descriptors; the path is
        resolved before admission but opened after it, so a swap while queued still lands.

        Args:
            notebook_id: The notebook ID.
            file_path: Path to the file to upload.
            mime_type: Content type for the upload handshake; inferred from the
                filename extension when omitted.
            title: Optional display title. When set and different from the
                filename, a rename is issued after upload (whitespace stripped;
                empty rejected). A non-default title forces a brief registration
                wait before the rename even when ``wait=False`` — UPDATE_SOURCE
                no-ops against an unregistered source (#388); a failed rename is
                logged and the filename title is kept.
            wait: If True, wait for the source to be fully ready before returning.
            wait_timeout: Max seconds to wait if ``wait=True`` (also bounds the
                narrow registration wait above). Default: 120.
            on_progress: Optional sync/async ``on_progress(bytes_sent, total)``
                callback during the upload body; its exceptions abort the upload.

        Returns:
            The created Source object; if wait=False, status may be PROCESSING.

        Raises:
            ValidationError: If the path is not a regular file, the title is
                empty, or the file is an HTML-family type the upload endpoint
                rejects (convert to text/Markdown/PDF first). A failure *after*
                registration raises its real type unwrapped, carrying
                ``source_id`` / ``stage`` attributes naming the retained row.
        """
        public_error: Exception | None = None
        try:
            result = await self._require_source_service().add_file(
                notebook_id,
                file_path,
                mime_type=mime_type,
                wait=wait,
                wait_timeout=wait_timeout,
                title=title,
                on_progress=on_progress,
            )
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            source = project_source(result.source)
            if wait:
                if result.transient_error_types is None:
                    source = await self.wait_until_ready(
                        notebook_id, source.id, timeout=wait_timeout
                    )
                else:
                    source = await self.wait_until_ready(
                        notebook_id,
                        source.id,
                        timeout=wait_timeout,
                        transient_error_types=result.transient_error_types,
                    )
                requested_title = title.strip() if title else ""
                if requested_title and source.title != requested_title:
                    finalized = await project_backend_call(
                        self._require_source_service().finalize_file_title(
                            notebook_id,
                            record_source(source),
                            requested_title,
                        )
                    )
                    return project_source(finalized.source)
                return source
            return source
        raise public_error

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

        Fires one ``GET_NOTEBOOK`` before the create for the retry probe (#2113);
        that read bumps Recent position (#2126). See the service method's docs.

        Args:
            notebook_id: The notebook ID.
            file_id: The Google Drive file ID.
            title: Display title. Drive imports re-derive it server-side, so a
                supplied ``title`` is honored via a follow-up :meth:`rename` (#1960).
            mime_type: Drive MIME type (Docs / Slides / Sheets /
                ``application/pdf``) — see :class:`~notebooklm.types.DriveMimeType`.
            wait: If True, wait for source to be ready before returning.
            wait_timeout: Maximum seconds to wait if wait=True (default: 120).

        Returns:
            The created Source object. If wait=False, status may be PROCESSING.

        Example:
            from notebooklm.types import DriveMimeType
            source = await client.sources.add_drive(notebook_id, file_id="1abc123xyz",
                title="My Document", mime_type=DriveMimeType.GOOGLE_DOC.value, wait=True)
        """
        public_error: Exception | None = None
        try:
            result = await self._require_source_service().add_drive(
                notebook_id,
                file_id,
                title,
                mime_type=mime_type,
                wait=wait,
                wait_timeout=wait_timeout,
                deadline=None,
            )
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            source = project_source(result.source)
            if wait:
                source = await self.wait_until_ready(
                    notebook_id,
                    source.id,
                    timeout=wait_timeout,
                )
                requested_title = title.strip()
                if requested_title and source.title != requested_title:
                    finalized = await project_backend_call(
                        self._require_source_service().finalize_drive_title(
                            notebook_id,
                            record_source(source),
                            requested_title,
                        )
                    )
                    return project_source(finalized.source)
                return source
            return source
        raise public_error

    async def add_drive_file(
        self,
        notebook_id: str,
        document_id: str,
        *,
        title: str | None = None,
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> Source:
        """Auto-route an upload-only Google Drive file: download it, then upload (#1884).

        Covers the upload-only Drive file types
        (epub/docx/pptx/txt/md/rtf/odt/csv/tsv/pdf; the rejection error names the
        full accepted set, derived from one declaration);
        a Drive PDF can also go by reference via :meth:`add_drive`. Fetches the file
        SERVER-SIDE using the same live ``.google.com`` cookie jar the upload leg
        uses (so it works in stdio AND remote MCP mode with no ``upload_required``
        detour), then streams it through :meth:`add_file`. Native Docs/Slides/
        Sheets are out of scope (not downloadable) — they raise a
        :class:`~notebooklm.exceptions.ValidationError` pointing at :meth:`add_drive`.

        Args:
            notebook_id: The notebook ID.
            document_id: A raw Drive file id or a Drive share URL (``/d/<id>``,
                ``/file/d/<id>/…``, or ``?id=<id>``).
            title: Optional display title; defaults to the file's Drive name.
            wait: If True, wait for the source to be ready before returning.
            wait_timeout: Maximum seconds to wait if ``wait=True`` (default: 120).

        Raises:
            ValidationError: unparseable id/URL, an upload-unsupported type
                (HTML/other), or a native (non-downloadable) Google Doc/Slides/Sheet.
        """
        public_error: Exception | None = None
        try:
            result = await self._require_source_service().add_drive_file(
                notebook_id,
                document_id,
                title=title,
                wait=wait,
                wait_timeout=wait_timeout,
            )
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            source = project_source(result.source)
            if wait:
                if result.transient_error_types is None:
                    source = await self.wait_until_ready(
                        notebook_id, source.id, timeout=wait_timeout
                    )
                else:
                    source = await self.wait_until_ready(
                        notebook_id,
                        source.id,
                        timeout=wait_timeout,
                        transient_error_types=result.transient_error_types,
                    )
                requested_title = title.strip() if title else (result.deferred_title or "")
                if requested_title and source.title != requested_title:
                    finalized = await project_backend_call(
                        self._require_source_service().finalize_file_title(
                            notebook_id,
                            record_source(source),
                            requested_title,
                        )
                    )
                    return project_source(finalized.source)
                return source
            return source
        raise public_error

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
        await project_backend_call(self._require_source_service().delete(notebook_id, source_id))

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
                ``False``, return ``None`` without hydrating. Miss-detection
                runs in both modes (``False`` returns ``None`` but raises a miss).

        Returns:
            The renamed :class:`~notebooklm.types.Source`, or ``None`` when
            ``return_object=False``.

        Raises:
            SourceNotFoundError: if the source does not exist (a content/list
                fetch, not a 404, detects it), in both ``return_object`` modes.

        .. versionchanged:: 0.7.0
            **Breaking change:** no longer fabricates an unverified
            ``Source(id, title)`` on a null echo; it hydrates and raises
            :class:`SourceNotFoundError` (#1255), plus ``return_object``.

        .. versionchanged:: 0.8.0
            **Breaking change:** ``return_object=False`` now runs the existence
            preflight on a null echo too, raising on a miss (#1362).
        """
        logger.debug("Renaming source %s to: %s", source_id, new_title)
        result = await project_backend_call(
            self._require_source_service().update(
                notebook_id,
                source_id,
                new_title,
                return_object=return_object,
            )
        )
        return project_source(result.source) if result.source is not None else None

    async def refresh(self, notebook_id: str, source_id: str) -> None:
        """Refresh a source to get updated content (for URL/Drive sources).

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to refresh.

        Returns:
            ``None`` on success; any failure raises first.

        .. versionchanged:: 0.8.0
            **Breaking change:** returns ``None`` (not always-``True``); the
            ``-> bool`` annotation is dropped (#1290).
        """
        await project_backend_call(self._require_source_service().refresh(notebook_id, source_id))
        return None

    async def check_freshness(self, notebook_id: str, source_id: str) -> bool:
        """Check if a source needs to be refreshed.

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to check.

        Returns:
            True if source is fresh, False if it needs refresh.

        Raises:
            DecodingError: If the freshness payload has a structurally
                unrecognized shape (schema drift) — so callers can tell a miss
                from drift instead of a silent "stale" (#1344).
        """
        return await project_backend_call(
            self._require_source_service().check_freshness(
                notebook_id,
                source_id,
            )
        )

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
                - ``keywords``: tuple of topic keyword strings

            Use attribute access (``guide.summary``, ``guide.keywords``).
        """
        result = await project_backend_call(
            self._require_source_service().get_guide(notebook_id, source_id)
        )
        return project_source_guide(result.guide)

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
            Source type codes include: 1=google_docs, 2=google_slides, 3=pdf,
            4=pasted_text, 5=web_page, 6=powerpoint, 8=markdown, 9=youtube,
            10=media, 11=docx, 13=image, 14=google_spreadsheet, 16=csv, 17=epub.

            The ``"markdown"`` format works by requesting the HTML rendition
            from the API (params ``[3],[3]`` instead of ``[2],[2]``) and
            converting it via *markdownify*.
        """
        result = await project_backend_call(
            self._require_source_service().get_fulltext(
                notebook_id,
                source_id,
                output_format=output_format,
            )
        )
        return project_source_fulltext(result.fulltext)

    # --- Private helper methods ---

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
        return extract_youtube_video_id(url, logger=logger)

    def _extract_video_id_from_parsed_url(self, parsed: Any, hostname: str) -> str | None:
        """Extract video ID from a parsed YouTube URL.

        Args:
            parsed: ParseResult from urlparse.
            hostname: Lowercase hostname.

        Returns:
            The raw video ID (not yet validated), or None.
        """
        return youtube_video_id_from_parsed_url(parsed, hostname)

    def _is_valid_video_id(self, video_id: str) -> bool:
        """Validate YouTube video ID format.

        YouTube video IDs contain only alphanumeric characters, hyphens,
        and underscores. They are typically 11 characters but can vary.

        Args:
            video_id: The video ID to validate.

        Returns:
            True if the video ID format is valid, False otherwise.
        """
        return is_valid_youtube_video_id(video_id)

    async def _add_youtube_source(self, notebook_id: str, url: str) -> Any:
        """Compatibility helper delegating to the semantic URL workflow."""
        return await self.add_url(notebook_id, url)

    async def _add_url_source(self, notebook_id: str, url: str) -> Any:
        """Compatibility helper delegating to the semantic URL workflow."""
        return await self.add_url(notebook_id, url)

    async def _register_file_source(self, notebook_id: str, filename: str) -> str:
        """Register a file source intent and get SOURCE_ID."""
        return await self._uploader.register_file_source(
            notebook_id,
            filename,
        )

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

        Thin delegator to :meth:`SourceUploadPipeline.upload_file_streaming`,
        which owns and documents the full contract: memory-safe streaming, the
        file-descriptor ownership transfer under the shielded finalize task, and
        the two-branch cancellation handling (in-flight finalize shielded +
        re-raised; pre-dispatch cancel fires a best-effort Scotty cancel POST).
        The legacy ``Path`` ``file_obj`` branch exists only for the direct-call
        unit tests in ``tests/unit/test_sources_upload.py``.

        Args:
            upload_url: The resumable upload URL from ``_start_resumable_upload``.
            file_obj: An open binary file object (ownership transfers to the
                pipeline) positioned at the bytes to upload, or a legacy ``Path``.
            filename: Optional filename for diagnostic logging.
            on_progress: Optional ``on_progress(bytes_sent, total_bytes)`` callback.
            total_bytes: Total bytes expected (required for the FD path; inferred
                from the path for legacy direct-call tests).
        """
        return await self._uploader.upload_file_streaming(
            upload_url,
            file_obj,
            filename=filename,
            on_progress=on_progress,
            total_bytes=total_bytes,
            logger=logger,
        )

    async def _cancel_upload_session(self, upload_url: str, auth_route: str) -> None:
        """Best-effort POST a Scotty resumable-upload cancel command.

        Invoked fire-and-forget (via ``asyncio.create_task``) from
        ``_upload_file_streaming`` when a ``CancelledError`` arrives
        BEFORE the finalize POST is dispatched, so the server-side
        session is torn down instead of held until Scotty's GC timeout.

        Network failures are swallowed — Ctrl-C cleanup is best-effort;
        the worst case is that the session lives until Scotty GCs it.
        Since the caller schedules this on a detached task, there is no outer
        await chain that can deliver a cancellation here, so no extra shield is
        needed at this layer. No base URL is passed: ``Origin``/``Referer`` are
        derived from the validated upload URL inside the pipeline.
        """
        await self._uploader.cancel_upload_session(
            upload_url,
            auth_route,
            logger=logger,
        )


_ORIGINAL_SOURCES_LIST: Final = SourcesAPI.list
_ORIGINAL_SOURCES_GET_OR_NONE: Final = SourcesAPI.get_or_none
