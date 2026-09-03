"""Web ``batchexecute`` source operations backend."""

import builtins
import logging
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from typing import IO, Any, Literal
from urllib.parse import urlparse

import httpx

from ..._runtime.call_supervisor import CallSupervisor
from ..._runtime.config import DEFAULT_MAX_CONCURRENT_UPLOADS
from ..._sources import SourcesAPI, validate_search
from ..._types.research import SourceGuide
from ..._types.sources import _EXPERT_INTELLIGENCE_TYPE_CODE
from ..._url_utils import is_youtube_url
from ...exceptions import SourceNotFoundError
from ...rpc import RPCMethod
from ...types import (
    CopiedSource,
    PlayBook,
    RelevantChunk,
    Source,
    SourceFulltext,
    SourceStatus,
    SourceType,
)
from ..contracts import RpcCaller
from ..params.sources import build_rename_source_params
from ..rows.sources import interpret_source_freshness
from ..settings import build_get_user_settings_params, extract_account_limits
from . import upload as _source_upload
from .add import (
    SourceAddService,
    _validate_add_text_idempotency,
    _validate_drive_file_id,
    honor_requested_title_if_fresh,
)
from .batch import SourceBatchAddService, SourceUrlBatchItem
from .content import SourceContentRenderer
from .listing import SourceLister
from .play_books import PlayBooksService
from .search import SourceSearchService
from .transfers import SourceTransferService
from .upload import SourceUploadPipeline

# Preserve the historical facade channel across the physical move.
logger = logging.getLogger("notebooklm._sources")

_SOURCE_ID_UUID_PATTERN = _source_upload._SOURCE_ID_UUID_PATTERN
_extract_register_file_source_id = _source_upload._extract_register_file_source_id
_looks_like_id_string = _source_upload._looks_like_id_string


class WebSourcesAPI(SourcesAPI):
    """Web ``batchexecute`` operations on NotebookLM sources.

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
        supervisor: CallSupervisor,
        uploader: SourceUploadPipeline,
        upload_timeout: httpx.Timeout | None = None,
        max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
    ):
        """Initialize the sources API.

        Args:
            rpc: The narrow :class:`RpcCaller` capability — sources
                only needs ``rpc_call(...)`` for its own RPC paths
                (delete, rename, refresh, freshness, drive add, text add).
                Upload-flow capabilities (``kernel`` and ``auth``) are owned
                by ``uploader``.
            supervisor: Client-wide logical-call supervisor. It owns admission
                for URL workflows that span multiple transport calls.
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
        # ``upload_timeout`` / ``max_concurrent_uploads`` are accepted for API
        # stability but honored by the injected ``uploader=`` pipeline (built by
        # the :class:`NotebookLMClient` composition root); stored here only as
        # historical attributes for callers that introspect the instance.
        self._rpc = rpc
        self._adder = SourceAddService()
        self._batch_adder = SourceBatchAddService()
        self._transfers = SourceTransferService()
        self._content = SourceContentRenderer(self._rpc, logger=logger)
        self._lister = SourceLister(self._rpc)
        self._play_books = PlayBooksService(self._rpc)
        self._searcher = SourceSearchService(self._rpc, logger=logger)
        super().__init__()
        self._upload_timeout = upload_timeout
        self._max_concurrent_uploads = max_concurrent_uploads
        self._supervisor = supervisor
        self._uploader = uploader
        self._uploader.configure_source_limit_lookup(self._get_source_limit)
        # Single owner for the source-lifecycle verbs: the upload pipeline reuses
        # the SAME ``SourceLister`` / ``SourcePoller`` instances this API uses for
        # its ``list_sources`` / ``get_source`` / ``wait_*`` verbs rather than
        # re-constructing parallel copies (issue #1205).
        self._uploader.configure_source_lifecycle(
            lister=self._lister,
            poller=self._poller,
        )

    async def _rpc_call(
        self,
        method: RPCMethod,
        params: builtins.list[Any],
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

    async def list(
        self,
        notebook_id: str,
        *,
        strict: bool = False,
        statuses: Collection[SourceStatus] | None = None,
        types: Collection[SourceType] | None = None,
    ) -> builtins.list[Source]:
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
        return await self._lister.list(
            notebook_id,
            strict=strict,
            statuses=statuses,
            types=types,
        )

    async def search(
        self,
        notebook_id: str,
        query: str,
        *,
        source_ids: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> builtins.list[RelevantChunk]:
        """Search indexed source passages by relevance.

        Args:
            notebook_id: Notebook containing the sources to search.
            query: Natural-language relevance query.
            source_ids: Optional source-id subset; omitted or empty searches
                every source in the notebook.
            limit: Optional positive maximum number of ranked chunks.

        Returns:
            Relevant chunks ordered by their global backend rank.
        """
        query, normalized_ids, limit = validate_search(query, source_ids, limit)
        return await self._searcher.search(
            notebook_id,
            query,
            source_ids=normalized_ids,
            limit=limit,
        )

    async def list_play_books(self) -> builtins.list[PlayBook]:
        """List Google Play Books eligible to be added as sources (#2292).

        Returns the account's "Expert Intelligence" library — purchased ebooks
        NotebookLM can ingest (US only, 18+). Empty for an account with no
        Play Books library. Titles with ``export_disabled`` set cannot be added;
        :meth:`add_play_book` refuses them client-side.
        """
        return await self._play_books.list_play_books()

    async def add_play_book(
        self,
        notebook_id: str,
        content_id: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> Source:
        """Add a Google Play Book as a source (#2292).

        Looks ``content_id`` up in :meth:`list_play_books` (which supplies the
        title, description, cover, authors and opaque ``field_type`` the add
        spec echoes back), refuses a non-exportable title with
        :class:`~notebooklm.exceptions.PlayBookNotExportableError`, then adds it
        via ``AddSourcesAsync``. The created source ingests as
        :attr:`~notebooklm.types.SourceType.EXPERT_INTELLIGENCE`.

        Args:
            notebook_id: The notebook ID.
            content_id: Play Books volume id (from a :class:`PlayBook`).
            wait: If True, wait for the source to be READY before returning.
            wait_timeout: Maximum seconds to wait if ``wait=True`` (default 120).

        Returns:
            The created :class:`Source`. With ``wait=False`` its status may be
            ``PROCESSING``.

        Raises:
            SourceNotFoundError: ``content_id`` is not in the library.
            PlayBookNotExportableError: the title cannot be exported.
        """
        async with self._supervisor.operation_scope("source.add_play_book"):
            books = await self._play_books.list_play_books()
            book = next((b for b in books if b.content_id == content_id), None)
            if book is None:
                raise SourceNotFoundError(
                    content_id,
                    method_id=RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT.value,
                )
            source_id = await self._play_books.add_play_book_spec(notebook_id, book)
            if wait:
                return await self.wait_until_ready(notebook_id, source_id, timeout=wait_timeout)
            # The add is confirmed (we hold ``source_id``). Return a PROCESSING
            # stub directly rather than an extra ``get_or_none`` read: that read
            # can raise on a transient transport/auth/decode fault (its contract
            # only returns ``None`` on a genuine miss), which would turn a
            # confirmed, non-idempotent add into an exception a caller might
            # retry — duplicating the source. A caller who wants the richer row
            # can poll ``get``/``list`` itself.
            return Source(
                id=source_id,
                title=book.title,
                _type_code=_EXPERT_INTELLIGENCE_TYPE_CODE,
                status=SourceStatus.PROCESSING,
            )

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
        async with self._supervisor.operation_scope("source.add_url"):
            result = await self._adder.add_url(
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
                return_result=True,
            )
            # Baseline-filtered probe ⇒ even a PROBED result is ours to rename (#2204).
            return await honor_requested_title_if_fresh(
                self.rename,
                notebook_id,
                result,
                title,
                logger,
                probe_proves_freshness=True,
            )

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
        async with self._supervisor.operation_scope("source.add_urls_batch"):
            return await self._batch_adder.add_urls(
                notebook_id,
                urls,
                rpc=self._rpc,
                list_sources=self.list,
                extract_youtube_video_id=self._extract_youtube_video_id,
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
        _validate_add_text_idempotency(idempotent)
        async with self._supervisor.operation_scope("source.add_text"):
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
        _validate_drive_file_id(file_id)
        async with self._supervisor.operation_scope("source.add_drive"):
            result = await self._adder.add_drive(
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
                return_result=True,
            )
            # Baseline-filtered probe ⇒ even a PROBED result is ours to rename (#2113).
            return await honor_requested_title_if_fresh(
                self.rename, notebook_id, result, title, logger, probe_proves_freshness=True
            )

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
        async with self._uploader.drive_download_scope(document_id) as (
            path,
            filename,
            content_type,
        ):
            return await self.add_file(
                notebook_id,
                path,
                mime_type=content_type,
                title=title if title else (filename or None),
                wait=wait,
                wait_timeout=wait_timeout,
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
        await self.delete_many(notebook_id, [source_id])

    async def delete_many(self, notebook_id: str, source_ids: Sequence[str]) -> None:
        """Delete sources in one ``DELETE_SOURCE`` RPC.

        Wire shape is a list of single-id lists: ``[[[id1], [id2], ...]]``.
        An empty ``source_ids`` is a no-op. Unknown ids are a silent no-op
        on the backend — callers that need ``not_found`` must resolve against
        a source-list snapshot first.
        """
        ids = list(dict.fromkeys(source_ids))
        if not ids:
            return
        logger.debug("Deleting %d source(s) from notebook %s", len(ids), notebook_id)
        await self._rpc.rpc_call(
            RPCMethod.DELETE_SOURCE,
            [[[sid] for sid in ids]],
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
        params = build_rename_source_params(source_id, new_title)
        result = await self._rpc.rpc_call(
            RPCMethod.UPDATE_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
        )
        if result and return_object:
            return Source.from_api_response(result, method_id=RPCMethod.UPDATE_SOURCE.value)
        # Null echo: hydrate via the internal lookup (never public ``get()`` —
        # #1247) so a miss raises; v0.8.0 (#1362) runs it to detect a miss.
        if not return_object and result:
            return None
        source = await self._get_or_none(notebook_id, source_id)
        if source is None:
            raise SourceNotFoundError(source_id, method_id=RPCMethod.UPDATE_SOURCE.value)
        return None if not return_object else source

    async def refresh(self, notebook_id: str, source_id: str) -> None:
        """Refresh a source to get updated content (for URL/Drive sources).

        Args:
            notebook_id: The notebook ID.
            source_id: The source ID to refresh.

        Returns:
            ``None`` on success; any failure raises first.

        Raises:
            RPCError: when the server rejects the call. ``REFRESH_SOURCE``
                answers a rejection as a null payload tagged with a gRPC
                status (live: ``[3]`` INVALID_ARGUMENT), and ``None`` is also
                the success value, so the status must raise or the two are
                indistinguishable (#2290).

        .. versionchanged:: 0.8.0
            **Breaking change:** returns ``None`` (not always-``True``); the
            ``-> bool`` annotation is dropped (#1290).

        .. versionchanged:: 0.9.0
            A server rejection raises :class:`RPCError` instead of returning
            ``None`` (#2290).
        """
        params = [None, [source_id], [2]]
        await self._rpc.rpc_call(
            RPCMethod.REFRESH_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            # The recorded success frame is a null payload with nothing at
            # index 5 (tests/cassettes/web/sources_refresh_direct.yaml), so
            # ``allow_null`` stays. ``raise_on_null_status`` is what separates
            # that from the ``[3]`` the server tags a rejection with (#2290) —
            # without it both decoded to ``None`` and this method reported
            # success for a live INVALID_ARGUMENT.
            allow_null=True,
            raise_on_null_status=True,
        )
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
        params = [None, [source_id], [2]]
        result = await self._rpc.rpc_call(
            RPCMethod.CHECK_SOURCE_FRESHNESS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return interpret_source_freshness(result)

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
            Source type codes include: 1=google_docs, 2=google_slides, 3=pdf,
            4=pasted_text, 5=web_page, 6=powerpoint, 8=markdown, 9=youtube,
            10=media, 11=docx, 13=image, 14=google_spreadsheet, 16=csv, 17=epub.

            The ``"markdown"`` format works by requesting the HTML rendition
            from the API (params ``[3],[3]`` instead of ``[2],[2]``) and
            converting it via *markdownify*.
        """
        return await self._content.get_fulltext(
            notebook_id,
            source_id,
            output_format=output_format,
        )

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
        # allow_null=False (mirrors _register_file_source): ADD_SOURCE returns the
        # new source row on success. A null result with a status code at wrb.fr[5]
        # is the #407 / #474 mode; allow_null=True would swallow that diagnostic,
        # so the decoder raises RPCError with the code for add_url to wrap.
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
        async with self._uploader.transport_operation_scope("upload-start") as epoch:
            return await self._uploader.start_resumable_upload(
                notebook_id,
                filename,
                file_size,
                source_id,
                content_type,
                expected_epoch=epoch,
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
        async with self._uploader.transport_operation_scope("upload-finalize") as epoch:
            return await self._uploader.upload_file_streaming(
                upload_url,
                file_obj,
                filename=filename,
                on_progress=on_progress,
                total_bytes=total_bytes,
                logger=logger,
                expected_epoch=epoch,
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
        async with self._uploader.transport_operation_scope("upload-cancel") as epoch:
            await self._uploader.cancel_upload_session(
                upload_url,
                auth_route,
                logger=logger,
                _expected_epoch=epoch,
            )

    # =========================================================================
    # Transfers (#2283): AddSourcesAsync / AppendSource / CopySourcesAsync
    # =========================================================================

    async def add_urls_async(
        self,
        notebook_id: str,
        urls: builtins.list[str],
    ) -> builtins.list[Source]:
        """Queue URL sources with one non-blocking ``AddSourcesAsync`` call.

        Same request as the batch ``ADD_SOURCE`` path, but the server answers
        as soon as the sources are queued (~0.65 s for two URLs versus ~2 s per
        synchronous add in the #2283 web probe) with stub rows — id, url and type only, status
        still processing. Poll :meth:`wait_until_ready` / :meth:`list` for the
        ingested rows.

        Never replayed on a transport failure: an unknown subset may have
        committed, so the error is marked unconfirmed for the caller to
        reconcile against :meth:`list`.

        .. versionadded:: 0.9.0
        """
        async with self._supervisor.operation_scope("source.add_urls_async"):
            return await self._transfers.add_urls_async(
                notebook_id,
                urls,
                rpc=self._rpc,
                extract_youtube_video_id=self._extract_youtube_video_id,
                logger=logger,
            )

    async def append_text(
        self,
        notebook_id: str,
        source_id: str,
        text: str,
        *,
        header: str = "",
    ) -> None:
        """Append a plain-text block to an existing source (``AppendSource``).

        ``text`` is appended at the very end of the source's fulltext (verified
        live: a 61-character pasted-text source grew to 86 characters ending in
        the appended block). ``header`` is accepted by the backend but does not
        appear in the fulltext. Success is an empty reply; a rejected call raises
        ``RPCError`` with the server status.

        .. versionadded:: 0.9.0
        """
        async with self._supervisor.operation_scope("source.append_text"):
            await self._transfers.append_text(
                notebook_id, source_id, text, header=header, rpc=self._rpc
            )

    async def copy(
        self,
        notebook_id: str,
        source_ids: builtins.list[str],
        target_notebook_id: str,
    ) -> builtins.list[CopiedSource]:
        """Copy sources into another notebook (``CopySourcesAsync``).

        Returns one :class:`~notebooklm.types.CopiedSource` per copied source,
        pairing the original id with the new row in ``target_notebook_id``
        (verified live by re-listing the target). An unknown source id or target
        notebook draws ``NOT_FOUND`` (``RPCError``); an empty mapping on success
        raises ``SourceNotFoundError`` so a no-op never reads as a copy. A partial
        result is returned with a warning because those copies have already
        committed.

        .. versionadded:: 0.9.0
        """
        async with self._supervisor.operation_scope("source.copy"):
            return await self._transfers.copy(
                notebook_id,
                source_ids,
                target_notebook_id,
                rpc=self._rpc,
                logger=logger,
            )


__all__ = ["WebSourcesAPI"]
