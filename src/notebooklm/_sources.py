"""Backend-neutral source operations API."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from typing import Any, Generic, Literal, Protocol, TypeVar

from ._lookup import unwrap_or_raise
from ._notebook_metadata import reconcile_copy_mapping
from ._runtime.call_supervisor import OperationLease
from ._source.batch import SourceUrlBatchItem
from ._source.polling import SourcePoller, SourceWaitResult
from ._types.research import SourceGuide
from .exceptions import (
    DecodingError,
    NetworkError,
    NonIdempotentRetryError,
    RPCError,
    SourceNotFoundError,
    ValidationError,
)
from .types import (
    CopiedSource,
    PlayBook,
    RelevantChunk,
    Source,
    SourceFulltext,
    SourceStatus,
    SourceType,
)

logger = logging.getLogger(__name__)

_TransferItem = TypeVar("_TransferItem")
_UploadedSourceWaiter = Callable[[str, str, float], Awaitable[Source]]
_UploadedSourceRenamer = Callable[[str, str, str], Awaitable[str | None]]


class _UploadedSourceFinalizer(Protocol):
    """Owner-neutral post-upload callback implemented by :class:`SourcesAPI`."""

    async def __call__(
        self,
        notebook_id: str,
        source_id: str,
        filename: str,
        *,
        wait: bool,
        wait_timeout: float,
        title: str | None,
        wait_until_ready: _UploadedSourceWaiter,
        wait_until_registered: _UploadedSourceWaiter,
        rename_uploaded: _UploadedSourceRenamer,
    ) -> Source: ...


@dataclass(frozen=True)
class _TransferResult(Generic[_TransferItem]):
    """Decoded rows plus backend-specific diagnostics for neutral policy."""

    items: builtins.list[_TransferItem]
    method_id: str
    malformed_count: int = 0
    raw_response: str | None = None


def _validate_add_text_idempotency(idempotent: bool) -> None:
    """Reject an unsupported text idempotency promise before admission."""
    if idempotent:
        raise NonIdempotentRetryError(
            "add_text cannot be marked idempotent: text sources have no "
            "reliable server-side dedupe key (titles non-unique, content "
            "not exposed). For idempotent text imports, embed a UUID in "
            "the title and dedupe client-side. See "
            "docs/python-api.md#idempotency."
        )


def validate_search(
    query: str,
    source_ids: Sequence[str] | None,
    limit: int | None,
) -> tuple[str, tuple[str, ...], int | None]:
    """Validate and normalize transport-neutral source-search inputs."""
    if not isinstance(query, str) or not query.strip():
        raise ValidationError("query must be a non-empty string")
    normalized_query = query.strip()

    if source_ids is None:
        normalized_ids: tuple[str, ...] = ()
    else:
        if isinstance(source_ids, (str, bytes)):
            raise ValidationError("source_ids must be a sequence of source IDs")
        deduplicated: list[str] = []
        seen: set[str] = set()
        for source_id in source_ids:
            if not isinstance(source_id, str) or not source_id:
                raise ValidationError("source_ids must contain only non-empty strings")
            if source_id not in seen:
                seen.add(source_id)
                deduplicated.append(source_id)
        normalized_ids = tuple(deduplicated)

    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
        raise ValidationError("limit must be a positive integer")
    return normalized_query, normalized_ids, limit


def finalize_search_results(
    chunks: Sequence[RelevantChunk],
    limit: int | None,
) -> list[RelevantChunk]:
    """Return globally ranked chunks, with unranked rows stable and last."""
    ranked = sorted(chunks, key=lambda chunk: (chunk.rank == 0, chunk.rank))
    return ranked if limit is None else ranked[:limit]


class SourcesAPI(ABC):
    """Backend-neutral operations on NotebookLM sources.

    Concrete backends own listing, mutation, content, and upload transport. The
    base owns identity lookup, post-upload finalization, and readiness polling
    over the abstract :meth:`list` operation.
    """

    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease | None]:
        """Return the backend's scope for one multi-call workflow."""

        return contextlib.nullcontext(None)

    def __init__(self) -> None:
        """Initialize transport-neutral source polling state."""
        self._poller = SourcePoller()

    @abstractmethod
    async def list(
        self,
        notebook_id: str,
        *,
        strict: bool = False,
        statuses: Collection[SourceStatus] | None = None,
        types: Collection[SourceType] | None = None,
    ) -> list[Source]:
        """List all sources in a notebook."""
        raise NotImplementedError

    @abstractmethod
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
            query: Natural-language relevance query; surrounding whitespace is
                ignored and a blank query is rejected.
            source_ids: Optional source-id filter. Duplicates are collapsed in
                first-seen order; ``None`` or an empty sequence searches all
                sources in the notebook.
            limit: Optional positive maximum number of chunks returned after
                global relevance ranking.

        Returns:
            Relevant source chunks ordered most-relevant first.
        """
        raise NotImplementedError

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
        for source in await self.list(notebook_id):
            if source.id == source_id:
                return source
        return None

    # Internal silent lookup for pollers/service code avoiding public ``get()`` misses.
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
        return await self._poller.wait_until_ready(
            notebook_id,
            source_id,
            timeout=timeout,
            initial_interval=initial_interval,
            max_interval=max_interval,
            backoff_factor=backoff_factor,
            transient_error_types=transient_error_types,
            get_source=self.get_or_none,
            sleep=asyncio.sleep,
            monotonic=monotonic,
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
    ) -> builtins.list[SourceWaitResult]:
        """Wait for many sources with ONE notebook snapshot per poll tick.

        Returns one result per id, in input order; terminal per-source failures
        (:class:`SourceNotFoundError` / :class:`SourceProcessingError` /
        :class:`SourceTimeoutError`) are RETURNED, not raised. See
        :meth:`SourcePoller.wait_all_until_ready`.
        """
        return await self._poller.wait_all_until_ready(
            notebook_id,
            source_ids,
            timeout=timeout,
            initial_interval=initial_interval,
            max_interval=max_interval,
            backoff_factor=backoff_factor,
            transient_error_types=transient_error_types,
            list_sources=self.list,
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
            get_source=self.get_or_none,
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

    @abstractmethod
    async def add_url(
        self,
        notebook_id: str,
        url: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        title: str | None = None,
    ) -> Source:
        """Add a URL source to a notebook."""
        raise NotImplementedError

    async def _add_urls_batch(
        self,
        notebook_id: str,
        urls: builtins.list[str],
    ) -> builtins.list[SourceUrlBatchItem]:
        """Backend adapter seam used by the existing MCP/REST batch endpoints."""
        raise NotImplementedError

    @abstractmethod
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
        """Add copied text to a notebook."""
        raise NotImplementedError

    @staticmethod
    async def _finalize_uploaded_file(
        notebook_id: str,
        source_id: str,
        filename: str,
        *,
        wait: bool,
        wait_timeout: float,
        title: str | None,
        wait_until_ready: _UploadedSourceWaiter,
        wait_until_registered: _UploadedSourceWaiter,
        rename_uploaded: _UploadedSourceRenamer,
    ) -> Source:
        """Apply backend-neutral readiness and display-title policy after upload."""
        needs_title_rename = title is not None and title != filename
        if wait:
            source = await wait_until_ready(notebook_id, source_id, wait_timeout)
        elif needs_title_rename:
            source = await wait_until_registered(notebook_id, source_id, wait_timeout)
        else:
            source = Source(
                id=source_id,
                title=filename,
                status=SourceStatus.PROCESSING,
                _type_code=None,
            )

        if needs_title_rename:
            try:
                assert title is not None
                echoed_title = await rename_uploaded(notebook_id, source_id, title)
                source = replace(source, title=echoed_title or title)
            except (RPCError, NetworkError):
                logger.warning(
                    "Source %s uploaded but title finalization failed",
                    source_id,
                )
        return source

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
        """Add a file source using the backend's resumable upload pipeline.

        Registers the source, opens an upload session, streams the file body,
        and applies a requested display title after upload. Uploads are
        memory-efficient and bounded by the backend's upload semaphore. The web
        pipeline resolves the path before semaphore admission, opens it only
        after admission, and may briefly wait for registration before applying
        a custom title because an early rename would no-op. Backend failures
        after registration retain their ``source_id`` and ``stage`` attributes.

        Args:
            notebook_id: The notebook ID.
            file_path: Path to the file to upload.
            mime_type: Content type for the upload handshake; inferred from the
                filename extension when omitted.
            title: Optional display title. Whitespace is stripped and an empty
                title is rejected. A failed best-effort rename keeps the
                upstream filename title.
            wait: If true, wait for the source to be fully ready.
            wait_timeout: Maximum seconds for readiness or title-registration
                waiting.
            on_progress: Optional sync or async callback receiving bytes sent
                and total bytes.

        Returns:
            The created source; when ``wait`` is false it may still be processing.

        Raises:
            ValidationError: If the path is not a regular file, the title is
                empty, or the file type is unsupported. A failure after
                registration retains its backend-specific ``source_id`` and
                ``stage`` diagnostics.
        """
        requested_title = None
        try:
            if title is not None:
                requested_title = title.strip()
                if not requested_title:
                    raise ValidationError("Title cannot be empty or whitespace-only")
            return await self._send_upload(
                notebook_id,
                file_path,
                mime_type,
                wait=wait,
                wait_timeout=wait_timeout,
                title=requested_title,
                on_progress=on_progress,
            )
        finally:
            # Android API objects own the bearer and transport pipeline. Do not
            # retain either owner (or caller-controlled values) in a published
            # exception's outer, backend-neutral traceback frame.
            del self, file_path, title, requested_title, on_progress

    @abstractmethod
    async def _send_upload(
        self,
        notebook_id: str,
        file_path: str | Path,
        mime_type: str | None,
        *,
        wait: bool,
        wait_timeout: float,
        title: str | None,
        on_progress: Callable[[int, int], object] | None,
    ) -> Source:
        """Run one backend upload pipeline around the shared finalization policy."""
        raise NotImplementedError

    @abstractmethod
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
        """Add a Google Drive document by reference."""
        raise NotImplementedError

    @abstractmethod
    async def list_play_books(self) -> builtins.list[PlayBook]:
        """List Google Play Books eligible to be added as sources (#2292)."""
        raise NotImplementedError

    @abstractmethod
    async def add_play_book(
        self,
        notebook_id: str,
        content_id: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> Source:
        """Add a Google Play Book as a source by its volume id (#2292)."""
        raise NotImplementedError

    @abstractmethod
    async def add_drive_file(
        self,
        notebook_id: str,
        document_id: str,
        *,
        title: str | None = None,
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> Source:
        """Download an upload-only Drive file and add it as a source."""
        raise NotImplementedError

    @abstractmethod
    async def delete(self, notebook_id: str, source_id: str) -> None:
        """Delete a source from a notebook."""
        raise NotImplementedError

    async def delete_many(self, notebook_id: str, source_ids: Sequence[str]) -> None:
        """Delete sources. Default: one :meth:`delete` per id.

        Web overrides this with a single ``DELETE_SOURCE`` RPC
        (``[[[id1], [id2], ...]]``). An empty ``source_ids`` is a no-op.
        Unknown ids are a silent no-op on the backend — callers that need
        ``not_found`` must resolve against a source-list snapshot first.
        """
        ids = list(dict.fromkeys(source_ids))
        for sid in ids:
            await self.delete(notebook_id, sid)

    @abstractmethod
    async def rename(
        self,
        notebook_id: str,
        source_id: str,
        new_title: str,
        *,
        return_object: bool = True,
    ) -> Source | None:
        """Rename a source."""
        raise NotImplementedError

    @abstractmethod
    async def refresh(self, notebook_id: str, source_id: str) -> None:
        """Refresh a URL or Drive source."""
        raise NotImplementedError

    @abstractmethod
    async def check_freshness(self, notebook_id: str, source_id: str) -> bool:
        """Return whether a source is fresh."""
        raise NotImplementedError

    @abstractmethod
    async def get_guide(self, notebook_id: str, source_id: str) -> SourceGuide:
        """Get the generated source guide."""
        raise NotImplementedError

    @abstractmethod
    async def get_fulltext(
        self,
        notebook_id: str,
        source_id: str,
        *,
        output_format: Literal["text", "markdown"] = "text",
    ) -> SourceFulltext:
        """Get the source content in text or Markdown form."""
        raise NotImplementedError

    @abstractmethod
    async def _send_add_urls_async(
        self,
        notebook_id: str,
        urls: builtins.list[str],
    ) -> _TransferResult[Source]:
        """Queue URL sources and return decoded rows plus the wire method id."""
        raise NotImplementedError

    @abstractmethod
    async def _send_append_text(
        self,
        notebook_id: str,
        source_id: str,
        text: str,
        *,
        header: str,
    ) -> None:
        """Append one text block through the selected backend."""
        raise NotImplementedError

    @abstractmethod
    async def _send_copy(
        self,
        notebook_id: str,
        source_ids: builtins.list[str],
        target_notebook_id: str,
    ) -> _TransferResult[CopiedSource]:
        """Copy sources and return decoded mappings plus the wire method id."""
        raise NotImplementedError

    async def add_urls_async(
        self,
        notebook_id: str,
        urls: builtins.list[str],
    ) -> builtins.list[Source]:
        """Queue URL sources without waiting for ingest (``AddSourcesAsync``).

        Returns the queued stub rows (id, url, type; status still processing)
        in request order. Unlike :meth:`add_url` this never probes-then-retries:
        a transport failure after the write may have committed an unknown
        subset, so it is surfaced as an unconfirmed error for the caller to
        reconcile against :meth:`list`.
        """
        if not urls:
            return []
        if any(not url or not url.strip() for url in urls):
            raise ValidationError("urls must not contain empty entries")
        async with self._operation_scope("source.add_urls_async"):
            transfer = await self._send_add_urls_async(notebook_id, urls)
        sources = transfer.items
        if not sources:
            raise DecodingError(
                "AddSourcesAsync returned no queued source rows",
                raw_response=transfer.raw_response,
                method_id=transfer.method_id,
            )
        if any(not source.id for source in sources):
            raise DecodingError(
                "AddSourcesAsync returned a queued source row without an id",
                raw_response=transfer.raw_response,
                method_id=transfer.method_id,
            )
        if len(sources) != len(urls):
            logger.warning(
                "AddSourcesAsync queued %d source(s) for %d URL(s) in notebook %s",
                len(sources),
                len(urls),
                notebook_id,
            )
        return sources

    async def append_text(
        self,
        notebook_id: str,
        source_id: str,
        text: str,
        *,
        header: str = "",
    ) -> None:
        """Append a plain-text block to an existing source in place (``AppendSource``).

        ``text`` lands at the very end of the source's fulltext; ``header`` is
        accepted by the backend but does not appear in the fulltext.
        """
        if not source_id:
            raise ValidationError("source_id must not be empty")
        if not text:
            raise ValidationError("text must not be empty")
        async with self._operation_scope("source.append_text"):
            await self._send_append_text(
                notebook_id,
                source_id,
                text,
                header=header,
            )

    async def copy(
        self,
        notebook_id: str,
        source_ids: builtins.list[str],
        target_notebook_id: str,
    ) -> builtins.list[CopiedSource]:
        """Copy sources from ``notebook_id`` into ``target_notebook_id`` (``CopySourcesAsync``).

        Returns one :class:`CopiedSource` per copied source, pairing the
        original id with the new row in the target notebook.
        """
        if not source_ids:
            raise ValidationError("source_ids must not be empty")
        if any(not source_id for source_id in source_ids):
            raise ValidationError("source_ids must not contain empty entries")
        if not target_notebook_id:
            raise ValidationError("target_notebook_id must not be empty")
        async with self._operation_scope("source.copy"):
            transfer = await self._send_copy(
                notebook_id,
                source_ids,
                target_notebook_id,
            )
        return reconcile_copy_mapping(
            source_ids,
            transfer.items,
            original_id=lambda item: item.original_id,
            operation="CopySourcesAsync",
            item_label="source",
            target_notebook_id=target_notebook_id,
            method_id=transfer.method_id,
            malformed_count=transfer.malformed_count,
            raw_response=transfer.raw_response,
            empty_error=SourceNotFoundError(", ".join(source_ids), method_id=transfer.method_id),
            warning_logger=logger,
        )


__all__ = ["SourcesAPI"]
