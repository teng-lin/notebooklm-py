"""Source operations API."""

import asyncio
import builtins
import logging
import os
import re
import warnings
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import IO, Any, Literal
from urllib.parse import parse_qs, urlparse

import httpx

from ._callbacks import maybe_await_callback
from ._capabilities import ClientCoreCapabilities
from ._core import ClientCore
from ._env import get_base_url
from ._idempotency import idempotent_create
from ._source_listing import SourceLister
from ._source_polling import SourcePoller
from ._url_utils import is_youtube_url
from .exceptions import (
    AuthError,
    NetworkError,
    NonIdempotentRetryError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from .rpc import RPCError, RPCMethod, get_upload_url
from .rpc.types import SourceStatus
from .types import (
    Source,
    SourceAddError,
    SourceFulltext,
    SourceNotFoundError,
    _extract_source_url,
)

logger = logging.getLogger(__name__)


_SOURCE_ID_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _extract_register_file_source_id(result: Any, filename: str) -> str | None:
    """Locate the SOURCE_ID string in an ADD_SOURCE_FILE response.

    The historical shape was a strictly position-0 walk: ``[[[[id]]]]``. Issue
    #474 surfaced cases where that walk lands on ``None`` or on the echoed
    filename and silently fails. Walk the whole structure instead, prefer a
    UUID-shaped leaf, and fall back to any other id-shaped string that is
    plausibly not a status label.
    """
    uuid_match: str | None = None
    fallback: str | None = None
    # Depth guard for malformed/adversarial payloads — Google's real responses
    # are shallow (≤5 levels), so 50 is generous without risking RecursionError.
    max_depth = 50

    def walk(node: Any, depth: int) -> None:
        nonlocal uuid_match, fallback
        if uuid_match is not None or depth > max_depth:
            return
        if isinstance(node, str):
            candidate = node.strip()
            if not candidate or candidate == filename:
                return
            if _SOURCE_ID_UUID_PATTERN.match(candidate):
                uuid_match = candidate
                return
            # Fallback: reject obvious non-id strings — status labels ("OK",
            # "DONE", "true"), mime types ("application/pdf"), free-form
            # messages. An id-shaped string has no embedded whitespace, no
            # slashes, is at least 4 chars, and contains at least one digit,
            # hyphen, or underscore (excludes all-alpha status tokens).
            if fallback is None and _looks_like_id_string(candidate):
                fallback = candidate
        elif isinstance(node, list):
            for child in node:
                if uuid_match is not None:
                    return
                walk(child, depth + 1)

    walk(result, 0)
    return uuid_match or fallback


def _looks_like_id_string(candidate: str) -> bool:
    """Heuristic for the non-UUID fallback in :func:`_extract_register_file_source_id`.

    Accepts strings that look like an id (`src_pdf`, `source_id_123`) and
    rejects short status tokens (`OK`, `DONE`, `true`) and structured fields
    (`application/pdf`, anything with whitespace).
    """
    if len(candidate) < 4:
        return False
    if any(c in candidate for c in " \t/"):
        return False
    # Require at least one digit, hyphen, or underscore — blocks all-alpha
    # status tokens while still admitting test-style ids like ``src_pdf``.
    return any(c.isdigit() or c in "-_" for c in candidate)


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
        self._lister = SourceLister(self._rpc_call)
        self._poller = SourcePoller()
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
        logger.debug("Adding URL source to notebook %s: %s", notebook_id, url[:80])
        video_id = self._extract_youtube_video_id(url)
        # Warn if URL looks like YouTube but we couldn't extract video ID
        if not video_id and is_youtube_url(url):
            logger.warning(
                "URL appears to be YouTube but no video ID found: %s. "
                "Adding as web page - content may be incomplete. "
                "If this is a video URL, please report this as a bug.",
                url[:100],
            )

        async def _create() -> Source:
            # Preserve transport-level signals so callers can act on the specific
            # type (AuthError → re-login, RateLimitError → back-off with retry_after,
            # ServerError → transient-retry). RateLimitError, ServerError, and
            # NetworkError must propagate so ``idempotent_create`` can catch
            # them and run the probe; AuthError continues to propagate to the
            # caller (auth failure cannot have committed the write). Only the
            # generic RPCError catch wraps into SourceAddError with the
            # underlying cause attached. Mirrors the propagation contract in
            # _register_file_source (#474, #407).
            try:
                if video_id:
                    result = await self._add_youtube_source(notebook_id, url)
                else:
                    result = await self._add_url_source(notebook_id, url)
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as e:
                raise SourceAddError(url, cause=e) from e

            if result is None:
                raise SourceAddError(url, message=f"API returned no data for URL: {url}")
            return Source.from_api_response(result)

        async def _probe() -> Source | None:
            # T7.B2: after a transport failure on ADD_SOURCE, list the
            # notebook's sources and check whether one with this exact URL
            # already exists. Best-effort — if listing fails, treat as
            # no-match so the wrapper retries the create.
            try:
                sources = await self.list(notebook_id)
            except Exception:
                logger.debug(
                    "add_url: probe list() failed; treating as no match",
                    exc_info=True,
                )
                return None
            for s in sources:
                if s.url == url:
                    return s
            return None

        source = await idempotent_create(
            _create,
            _probe,
            label=f"sources.add_url[{url[:40]}]",
        )

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
        logger.debug("Adding file source to notebook %s: %s", notebook_id, file_path)
        # DEPRECATION-REMOVAL: v0.X.0 — the ``mime_type`` argument is unused by
        # the resumable-upload pipeline (the server derives the MIME type from
        # the filename extension). Kept as a positional kwarg for backward
        # compatibility; callers passing a non-None value get a warning. See
        # CHANGELOG entry "Deprecated: ``SourcesAPI.add_file`` ``mime_type``
        # parameter" under ``[Unreleased]`` for the planned removal release.
        if mime_type is not None:
            warnings.warn(
                "mime_type parameter is unused and will be removed in v0.X.0; "
                "rely on filename extension instead",
                DeprecationWarning,
                stacklevel=2,
            )
        if title is not None:
            title = title.strip()
            if not title:
                raise ValidationError("Title cannot be empty or whitespace-only")

        file_path = Path(file_path).resolve()

        # Cheap pre-check. The real existence + regular-file check is
        # ``open()`` itself (errors with ``FileNotFoundError`` / ``IsADirectoryError``);
        # these probes give a clearer up-front error AND short-circuit
        # before we acquire the upload semaphore. They can't replace
        # the FD-level check below because the file can be swapped between
        # these probes and the ``open()``; that's the TOCTOU the FD-hold
        # fix closes.
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        if not file_path.is_file():
            raise ValidationError(f"Not a regular file: {file_path}")

        filename = file_path.name

        # Step 0–3 run under the upload semaphore so a fan-out caller can't
        # hold more than ``max_concurrent_uploads`` open FDs at once. The
        # semaphore is per-instance; see ClientCore.get_upload_semaphore.
        upload_sem = self._capabilities.get_upload_semaphore()
        upload_wait_start = monotonic()
        async with upload_sem:
            self._capabilities.record_upload_queue_wait(monotonic() - upload_wait_start)
            # Open the file ONCE here. The FD lives across:
            #   - the os.fstat() size check (so the size we send to
            #     ``_start_resumable_upload`` matches the bytes we will
            #     actually stream),
            #   - the two RPC calls (registration + upload-session start),
            #   - the streamed body POST inside ``_upload_file_streaming``.
            # A racing rename/replace of the path between any of these
            # points cannot swap the FD's underlying inode out from under
            # us — closing the TOCTOU window the pre-T7.D3 implementation
            # had between its ``stat()``-based size probe and the second
            # ``open()`` inside the streaming helper (audit §23).
            #
            # FD ownership handoff:
            # ``add_file`` opens the FD here and closes it on any
            # exception raised by the size check OR the two RPCs (the
            # ``except BaseException`` branch below). The moment
            # ``_upload_file_streaming`` is invoked, ownership transfers
            # to the streaming helper, which arranges close-on-done via
            # ``add_done_callback`` on the shielded finalize task. The
            # transfer is necessary because T7.C3 keeps the shielded
            # POST running in the background after a post-finalize
            # cancel — if ``add_file`` closed the FD here, the background
            # POST would read from a closed FD and abort, breaking the
            # T7.C3 dangling-session guarantee.
            # noqa SIM115: a ``with open(...)`` would close the FD on
            # exit of the block, but ``_upload_file_streaming`` takes
            # ownership of the FD via its shielded done-callback (see
            # the FD-handoff comment above). We close locally only when
            # the handoff never happens (``handed_off=False`` branch in
            # the ``finally`` below).
            file_obj = open(file_path, "rb")  # noqa: SIM115
            handed_off = False
            operation_token = None
            try:
                # ``os.fstat(fd.fileno())`` reads inode metadata via the
                # FD itself, not the path — even if the path has been
                # relinked since ``open()``, this returns the inode we're
                # about to upload.
                file_size = os.fstat(file_obj.fileno()).st_size
                operation_token = await self._core._begin_transport_post(
                    f"source upload {filename}"
                )

                # Step 1: Register source intent with RPC → get SOURCE_ID
                source_id = await self._register_file_source(notebook_id, filename)

                # Step 2: Start resumable upload with the SOURCE_ID from step 1
                upload_url = await self._start_resumable_upload(
                    notebook_id, filename, file_size, source_id
                )

                # Step 3: Stream upload file content (memory-efficient).
                # Ownership of ``file_obj`` transfers to
                # ``_upload_file_streaming`` here — the helper closes it
                # on shielded-task completion (success, error, or the
                # post-finalize cancel branch). ``handed_off=True``
                # prevents the local ``finally`` from double-closing.
                handed_off = True
                await self._upload_file_streaming(
                    upload_url,
                    file_obj,
                    filename=filename,
                    on_progress=on_progress,
                    total_bytes=file_size,
                )
            finally:
                # Close locally ONLY if ownership never transferred —
                # i.e. ``_upload_file_streaming`` was never invoked
                # (size-check or RPC raised). After hand-off the
                # streaming helper owns the close.
                try:
                    if operation_token is not None:
                        await self._core._finish_transport_post(operation_token)
                finally:
                    if not handed_off:
                        file_obj.close()

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
        """Add a YouTube video as a source.

        ``disable_internal_retries=True`` (T7.B2): ADD_SOURCE is a
        mutating RPC that may have committed server-side even if the
        client sees a 5xx / network error. The probe-then-retry loop
        in ``add_url`` owns recovery via ``idempotent_create``.
        """
        params = [
            [[None, None, None, None, None, None, None, [url], None, None, 1]],
            notebook_id,
            [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]
        # allow_null=False mirrors _register_file_source — ADD_SOURCE on
        # success returns the new source row. A null result with a status
        # code at wrb.fr[5] is the #407 / #474 mode; allow_null=True would
        # swallow that diagnostic. The decoder now raises RPCError with the
        # status code so add_url can wrap it into SourceAddError with detail.
        return await self._core.rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=False,
            disable_internal_retries=True,
        )

    async def _add_url_source(self, notebook_id: str, url: str) -> Any:
        """Add a regular URL as a source.

        ``disable_internal_retries=True`` (T7.B2): see
        ``_add_youtube_source`` for the rationale.
        """
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
            disable_internal_retries=True,
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

        # allow_null=False: ADD_SOURCE_FILE should always return the source id
        # on success. When the server quietly returns null with a status code
        # at wrb.fr[5] — the suspected #474 mode for account-routing mismatches
        # (issues #114, #294) — the decoder enriches the error with that code
        # and an account-routing hint. Surface that diagnostic to the caller
        # via SourceAddError, instead of swallowing the null with allow_null=True
        # and raising a generic "Failed to get SOURCE_ID" with no detail.
        #
        # AuthError, RateLimitError, and ServerError are allowed to propagate
        # unchanged so callers can keep using their specific exception types
        # for auth-refresh retry, rate-limit back-off, and server-error handling
        # without having to unwrap SourceAddError.cause.
        try:
            result = await self._core.rpc_call(
                RPCMethod.ADD_SOURCE_FILE,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=False,
            )
        except (AuthError, RateLimitError, ServerError):
            raise
        except RPCError as exc:
            raise SourceAddError(
                filename,
                cause=exc,
                message=f"Failed to register file source for {filename}: {exc}",
            ) from exc

        source_id = _extract_register_file_source_id(result, filename)
        if source_id:
            return source_id

        # The decoder returned a non-null payload that the walker couldn't
        # parse — a genuine shape drift, not a null-result rejection. Include
        # a faithful preview of the actual response (repr, not json — repr
        # surfaces types the walker rejected) so future drift produces an
        # actionable bug report (#474).
        preview = repr(result)
        if len(preview) > 200:
            preview = preview[:200] + "..."
        raise SourceAddError(
            filename,
            message=(
                f"Failed to get SOURCE_ID from registration response. Response shape: {preview}"
            ),
        )

    async def _start_resumable_upload(
        self,
        notebook_id: str,
        filename: str,
        file_size: int,
        source_id: str,
    ) -> str:
        """Start a resumable upload session and get the upload URL."""
        import json

        auth_route = self._capabilities.authuser_header()
        base_url = get_base_url()
        url = f"{get_upload_url()}?{self._capabilities.authuser_query()}"

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
            timeout=self._resolve_upload_timeout(httpx.Timeout(10.0, read=60.0)),
            cookies=self._capabilities.live_cookies(),
        ) as client:
            response = await client.post(url, headers=headers, content=body)
            response.raise_for_status()

            upload_url = response.headers.get("x-goog-upload-url")
            if not upload_url:
                raise SourceAddError(
                    filename, message="Failed to get upload URL from response headers"
                )

            return upload_url

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
        # Discriminate: caller passed an open FD (the T7.D3 add_file path),
        # or a Path (legacy direct-call test path — opens locally). The
        # ``add_file`` callsite always takes the FD branch; the Path branch
        # exists only so the existing _upload_file_streaming unit tests
        # don't need to set up their own ``open()`` machinery.
        path_fallback: Path | None = file_obj if isinstance(file_obj, Path) else None
        # The "donecallback wired" sentinel — flipped to True only AFTER
        # ``add_done_callback`` registers the FD-close hook on
        # ``finalize_task``. If the function raises BEFORE that flip,
        # we synchronously close the FD ourselves so a caller that
        # transferred ownership doesn't leak.
        close_wired = False
        try:
            base_url = get_base_url()
            auth_route = self._capabilities.authuser_header()
            headers = {
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                "x-goog-authuser": auth_route,
                "Origin": base_url,
                "Referer": f"{base_url}/",
                "x-goog-upload-command": "upload, finalize",
                "x-goog-upload-offset": "0",
            }
            diag_name = filename or (path_fallback.name if path_fallback is not None else "<file>")
            logger.debug("Streaming upload to %s for %s", upload_url, diag_name)
            if total_bytes is None and path_fallback is not None:
                total_bytes = path_fallback.stat().st_size
            progress_total = total_bytes if total_bytes is not None else 0
            uploaded_bytes = 0

            if on_progress is not None:
                await maybe_await_callback(on_progress, uploaded_bytes, progress_total)

            # Stream the file content instead of loading it all into memory.
            # When the caller passed an FD, we read directly from it (single
            # open() per add_file call). When the caller passed a Path
            # (legacy direct-call), we open here as a one-off helper whose
            # ``with`` closes the locally-opened FD when the generator ends.
            async def file_stream():
                nonlocal uploaded_bytes
                # T7.D2 / audit §22: chunk reads are synchronous file I/O.
                # On slow storage (network FS, encrypted home, large PDFs
                # served from a cold cache) a bare ``f.read(65536)`` from
                # an ``async`` context stalls the entire event loop for
                # the duration of each chunk read. Wrap each read with
                # ``asyncio.to_thread`` so the blocking syscall runs on
                # the default thread executor and sibling tasks (auth
                # refresh, heartbeats, the cancellation watchdog above)
                # keep making progress while bytes stream in.
                if path_fallback is not None:
                    with open(path_fallback, "rb") as f:
                        while chunk := await asyncio.to_thread(f.read, 65536):  # 64KB chunks
                            uploaded_bytes += len(chunk)
                            if on_progress is not None:
                                await maybe_await_callback(
                                    on_progress, uploaded_bytes, progress_total
                                )
                            yield chunk
                    return
                # FD path: caller transferred ownership to this helper; we
                # do NOT use a ``with`` here — the FD is closed by the
                # done-callback on ``finalize_task`` below so the shielded
                # background task can still read from it under post-finalize
                # cancel.
                assert not isinstance(file_obj, Path)  # narrowed by branch above
                while chunk := await asyncio.to_thread(file_obj.read, 65536):  # 64KB chunks
                    uploaded_bytes += len(chunk)
                    if on_progress is not None:
                        await maybe_await_callback(on_progress, uploaded_bytes, progress_total)
                    yield chunk

            # `finalize_started` flips after the local ``httpx.AsyncClient``
            # context enters and immediately before ``client.post(...)``. There
            # is no ``await`` between the flip and the POST, so asyncio cannot
            # deliver a cancel in that window — once the flag is True, the
            # request is effectively in flight. On cancel we discriminate:
            #   - True  → shield is keeping the in-flight POST alive; just re-raise.
            #   - False → no POST went out, so fire a best-effort Scotty cancel.
            finalize_started = False

            async def _do_finalize() -> None:
                nonlocal finalize_started
                # See _start_resumable_upload: pass the live cookie jar so
                # httpx scopes cookies per Domain attribute. Scotty validates
                # OSID against host and rejects foreign-host cookies. (#373)
                async with httpx.AsyncClient(
                    timeout=self._resolve_upload_timeout(httpx.Timeout(10.0, read=300.0)),
                    cookies=self._capabilities.live_cookies(),
                ) as client:
                    finalize_started = True
                    response = await client.post(upload_url, headers=headers, content=file_stream())
                    response.raise_for_status()

            def _on_finalize_done(t: asyncio.Task[None]) -> None:
                # Close the FD owned-by-this-helper (T7.D3 ownership transfer).
                # Fires whether the task completed normally, raised, or was
                # cancelled — including the post-finalize background-drain
                # branch where the caller is long gone. The Path-branch
                # closes its FD inside the generator's ``with`` block and
                # does NOT need this hook.
                if path_fallback is None:
                    # path_fallback is None ⇔ file_obj is not a Path (see
                    # line 1545 where path_fallback is derived).
                    try:
                        file_obj.close()  # type: ignore[union-attr]
                    except Exception as close_exc:  # noqa: BLE001 — defensive
                        # Already-closed / detached FD: harmless. Log at debug
                        # so a real misconfiguration is still discoverable.
                        logger.debug("Caller FD close in finalize-done failed: %r", close_exc)
                # On post-finalize cancel, ``finalize_task`` keeps running in
                # the background. Without this callback, an unawaited
                # exception (e.g. server 5xx) would surface as a noisy
                # "Task exception was never retrieved" asyncio warning.
                if not t.cancelled() and (exc := t.exception()) is not None:
                    logger.debug("Background finalize POST failed: %r", exc)

            finalize_task = asyncio.create_task(_do_finalize())
            finalize_task.add_done_callback(_on_finalize_done)
            # FD-close is now wired to fire on task completion. Even if the
            # ``await asyncio.shield(...)`` below raises, the done-callback
            # will close the caller-owned FD when the (possibly cancelled,
            # possibly shielded-into-the-background) task transitions to
            # done. From this point we no longer need the synchronous
            # ``finally`` fallback.
            close_wired = True
            try:
                await asyncio.shield(finalize_task)
            except asyncio.CancelledError:
                if not finalize_started:
                    # Pre-finalize cancel: the POST never went out. Cancel
                    # the still-setting-up inner task and fire a best-effort
                    # Scotty cancel so the resumable upload session doesn't
                    # dangle until the server's GC sweeps it.
                    finalize_task.cancel()
                    asyncio.create_task(
                        self._cancel_upload_session(upload_url, base_url, auth_route)
                    )
                # Post-finalize cancel: asyncio.shield is keeping the inner
                # task alive; let it run to completion in the background,
                # then propagate the cancel as the caller requested.
                raise
        except BaseException:
            # Pre-task-creation raise (or pre-``add_done_callback`` raise):
            # nothing else will close the caller-owned FD. Do it
            # synchronously so a transferred FD doesn't leak. Once the
            # done-callback is wired (``close_wired=True``), the task's
            # done-callback handles close on every termination path,
            # so we skip the local close to avoid double-close.
            if not close_wired and path_fallback is None:
                # path_fallback is None ⇔ file_obj is not a Path.
                try:
                    file_obj.close()  # type: ignore[union-attr]
                except Exception as close_exc:  # noqa: BLE001 — defensive
                    logger.debug("Caller FD close on pre-wire exception failed: %r", close_exc)
            raise

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
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "x-goog-authuser": auth_route,
            "Origin": base_url,
            "Referer": f"{base_url}/",
            "x-goog-upload-command": "cancel",
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, read=10.0),
                cookies=self._capabilities.live_cookies(),
            ) as client:
                await client.post(upload_url, headers=headers)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.debug("Best-effort Scotty cancel for %s failed: %r", upload_url, exc)
