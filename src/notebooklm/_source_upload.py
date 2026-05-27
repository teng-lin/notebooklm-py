"""Private source file upload pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from typing import IO, TYPE_CHECKING, Any, Protocol, cast

import httpx

from ._callbacks import maybe_await_callback
from ._env import get_base_url
from ._idempotency import idempotent_create
from ._session_config import (
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    normalize_max_concurrent_uploads,
)
from ._session_contracts import (
    AuthMetadata,
    Kernel,
    LoopGuard,
    OperationScopeProvider,
    RpcCaller,
)
from ._source_listing import SourceLister
from ._source_polling import SourcePoller
from .auth import authuser_query, format_authuser_value
from .exceptions import (
    AuthError,
    NetworkError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from .rpc import RPCError, RPCMethod, get_upload_url
from .rpc.types import SourceStatus
from .types import Source, SourceAddError

if TYPE_CHECKING:
    from ._session_lifecycle import ClientLifecycle
    from ._transport_drain import TransportDrainTracker

_SOURCE_ID_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class RpcCallback(Protocol):
    """RPC callback shape used by upload registration.

    Structurally distinct from :class:`notebooklm._session_contracts.RpcCaller`:
    this is a **callable** Protocol (``async def __call__(...)``) passed as a
    keyword argument into :meth:`SourceUploadPipeline.register_file_source`,
    while the shared ``RpcCaller`` is an **object** Protocol with an
    ``.rpc_call(...)`` method. They are NOT interchangeable — the local
    callable form is kept as a structural Protocol (not a ``Callable[...]``
    alias) so mypy can flag keyword-name typos at call sites.
    """

    async def __call__(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any: ...


GetSourceLimit = Callable[[], Awaitable[int | None]]


class UploadRuntime(RpcCaller, OperationScopeProvider, LoopGuard, Protocol):
    """Runtime capabilities required by source upload.

    Combines :class:`RpcCaller` (``rpc_call`` method),
    :class:`OperationScopeProvider` (``operation_scope`` async-context
    manager), and :class:`LoopGuard` (``assert_bound_loop`` method) — the
    only ``Session`` surfaces the pipeline needs at runtime. A concrete
    :class:`Session` structurally satisfies this Protocol.

    Audit C1: ``LoopGuard`` is included so :meth:`SourceUploadPipeline.add_file`
    can short-circuit cross-loop misuse *before* entering
    ``operation_scope`` or lazily allocating the per-loop upload semaphore.
    Mirrors the ``ArtifactsRuntime`` pattern. (The historical
    ``ChatRuntime`` Protocol that this docstring also referenced was
    deleted in Wave 8 of the session-decoupling plan, ADR-014 Rule 2
    Corollary, in favour of direct constructor injection on ``ChatAPI``.)
    """


@dataclass(frozen=True)
class UploadRuntimeAdapter:
    """Concrete satisfier of :class:`UploadRuntime` per ADR-014 Rule 2.

    Earns its keep under Rule 2 because:

    - the composite has three capabilities (``RpcCaller`` +
      ``OperationScopeProvider`` + ``LoopGuard``);
    - :class:`SourceUploadPipeline` takes the whole composite as a
      single dependency (``runtime``) and threads it into the
      lister/poller collaborators — consumer-demand;
    - ``assert_bound_loop`` is a meaningful named affordance used as a
      cross-loop short-circuit guard in :meth:`SourceUploadPipeline.add_file`.

    The adapter is constructed at the composition root
    (:class:`notebooklm.client.NotebookLMClient`) from the three
    underlying collaborators: :class:`RpcCaller`,
    :class:`TransportDrainTracker`, and :class:`ClientLifecycle`. It
    transitively satisfies ``AsyncWorkRuntime`` (``LoopGuard`` +
    ``OperationScopeProvider``) per ADR-014 Rule 2 — no dedicated
    ``_AsyncWorkAdapter`` is introduced.

    :class:`SourceUploadPipeline` continues to take :class:`Kernel`
    and :class:`AuthMetadata` as separate parameters, so this adapter
    covers only the composite ``UploadRuntime`` Protocol — mirroring
    the ADR-014 Rule 6 example.
    """

    rpc: RpcCaller
    drain: TransportDrainTracker
    lifecycle: ClientLifecycle

    async def rpc_call(
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
        return await self.rpc.rpc_call(
            method,
            params,
            source_path,
            allow_null,
            _is_retry,
            disable_internal_retries=disable_internal_retries,
            operation_variant=operation_variant,
        )

    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]:
        return self.drain.operation_scope(label)

    def assert_bound_loop(self) -> None:
        self.lifecycle.assert_bound_loop()


_INVALID_ARGUMENT_RPC_CODE = 3
_SOURCE_LIMIT_HINT_FLOOR = 50
_TIER_SOURCE_LIMITS_SUMMARY = "50/100/300/600"
# Preserve the historical ``notebooklm._sources`` log channel after moving
# upload choreography into this module.
module_logger = logging.getLogger("notebooklm").getChild("_sources")


async def _build_invalid_argument_source_limit_hint(
    *,
    source_count: int | None,
    get_source_limit: GetSourceLimit | None,
    logger: Any,
) -> str:
    """Build a best-effort hint for ADD_SOURCE_FILE status code 3 failures."""
    source_limit: int | None = None
    if get_source_limit is not None:
        try:
            source_limit = await get_source_limit()
        except Exception:  # noqa: BLE001 - hint lookup must not mask the upload error.
            logger.debug(
                "register_file_source: source-limit lookup failed; continuing without limit hint",
                exc_info=True,
            )

    if source_limit is not None and source_limit <= 0:
        source_limit = None

    if source_count is not None and source_limit is not None:
        if source_count >= source_limit:
            return (
                f" Notebook currently has {source_count}/{source_limit} sources, "
                "so this likely means the notebook has reached its tier-specific "
                "per-notebook source limit. Delete sources or try a fresh notebook, "
                "then retry."
            )
        return (
            f" Notebook currently has {source_count}/{source_limit} sources, below "
            "the advertised account limit. If the file is valid, try the same add "
            "in a fresh notebook to distinguish file rejection from notebook state."
        )

    if source_count is not None and source_count >= _SOURCE_LIMIT_HINT_FLOOR:
        return (
            f" Notebook currently has {source_count} sources; status code 3 can "
            "indicate the notebook is at or near the tier-specific per-notebook "
            f"source limit ({_TIER_SOURCE_LIMITS_SUMMARY}). Delete sources or "
            "try a fresh notebook, then retry."
        )

    if source_limit is not None:
        return (
            f" Advertised source limit for this tier is {source_limit}; compare "
            "it with this notebook's source count. Status code 3 can indicate a "
            "per-notebook source-limit rejection."
        )

    return ""


class AsyncClientFactory(Protocol):
    """Factory for creating an ``httpx.AsyncClient``-compatible instance."""

    def __call__(
        self,
        *,
        timeout: httpx.Timeout,
        cookies: httpx.Cookies,
    ) -> httpx.AsyncClient: ...


ListSources = Callable[[str], Awaitable[list[Source]]]
QueueWaitRecorder = Callable[[float], None]

_MEDIA_CONTENT_TYPE_PREFIXES = ("audio/", "video/")
_MEDIA_APPLICATION_CONTENT_TYPES = frozenset(
    {
        "application/mp4",
        "application/ogg",
        "application/x-matroska",
    }
)
_MEDIA_TRANSIENT_ERROR_TYPES: tuple[int | None, ...] = (10, 0, None)
_STRICT_TRANSIENT_ERROR_TYPES: tuple[int | None, ...] = ()


# Audit CC6: single-loop-per-client invariant per ADR-004; not safe for multi-loop fan-out.
_BACKGROUND_CANCEL_TASKS: set[asyncio.Task[None]] = set()


def _retain_background_cancel_task(task: asyncio.Task[None]) -> None:
    _BACKGROUND_CANCEL_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_CANCEL_TASKS.discard)


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
    # Depth guard for malformed/adversarial payloads. Google's real responses
    # are shallow, so 50 is generous without risking RecursionError.
    max_depth = 50

    def walk(node: Any, depth: int) -> None:
        nonlocal uuid_match, fallback
        if uuid_match is not None or depth > max_depth:
            return
        if isinstance(node, str):
            if len(node) > 1000:
                return
            candidate = node.strip()
            if not candidate or candidate == filename:
                return
            if _SOURCE_ID_UUID_PATTERN.match(candidate):
                uuid_match = candidate
                return
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
    """Heuristic for the non-UUID fallback in file-source id extraction."""
    if len(candidate) < 4:
        return False
    if any(c in candidate for c in " \t/"):
        return False
    return any(c.isdigit() or c in "-_" for c in candidate)


def _resolve_upload_content_type(file_path: Path, mime_type: str | None) -> str:
    """Return the content type for the Scotty resumable-upload start request."""
    if mime_type is not None:
        content_type = mime_type.strip()
        if not content_type:
            raise ValidationError("mime_type cannot be empty or whitespace-only")
        return content_type

    guessed, _encoding = mimetypes.guess_type(file_path.name)
    return guessed or "application/octet-stream"


def _transient_error_types_for_upload(content_type: str) -> tuple[int | None, ...]:
    """Return source status=ERROR transient policy for this upload."""
    normalized = content_type.split(";", 1)[0].strip().lower()
    if (
        normalized.startswith(_MEDIA_CONTENT_TYPE_PREFIXES)
        or normalized in _MEDIA_APPLICATION_CONTENT_TYPES
    ):
        return _MEDIA_TRANSIENT_ERROR_TYPES
    return _STRICT_TRANSIENT_ERROR_TYPES


class SourceUploadPipeline:
    """Own file registration and resumable upload orchestration."""

    def __init__(
        self,
        runtime: UploadRuntime,
        kernel: Kernel,
        auth: AuthMetadata,
        upload_timeout: httpx.Timeout | None = None,
        max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
        *,
        record_upload_queue_wait: QueueWaitRecorder | None = None,
        async_client_factory: AsyncClientFactory | None = None,
        get_source_limit: GetSourceLimit | None = None,
    ):
        self._runtime = runtime
        self._kernel = kernel
        self._auth = auth
        self._upload_timeout = upload_timeout
        self._record_upload_queue_wait = record_upload_queue_wait
        self._async_client_factory = async_client_factory
        self._max_concurrent_uploads = normalize_max_concurrent_uploads(max_concurrent_uploads)
        self._upload_semaphore: asyncio.Semaphore | None = None
        self._lister = SourceLister(self._runtime)
        self._poller = SourcePoller()
        self._get_source_limit = get_source_limit

    def configure_source_limit_lookup(self, get_source_limit: GetSourceLimit | None) -> None:
        """Set the optional source-limit lookup used in registration hints."""
        self._get_source_limit = get_source_limit

    def _resolve_upload_timeout(self, default: httpx.Timeout) -> httpx.Timeout:
        """Return the configured upload timeout, or ``default`` if unset."""
        return self._upload_timeout if self._upload_timeout is not None else default

    def _client_factory(self) -> AsyncClientFactory:
        return self._async_client_factory or httpx.AsyncClient

    def _authuser_query(self) -> str:
        return authuser_query(self._auth.authuser, self._auth.account_email)

    def _authuser_header(self) -> str:
        return format_authuser_value(self._auth.authuser, self._auth.account_email)

    def _live_cookies(self) -> httpx.Cookies:
        cookies = getattr(self._kernel, "cookies", None)
        if isinstance(cookies, httpx.Cookies):
            return cookies
        get_http_client = getattr(self._kernel, "get_http_client", None)
        if get_http_client is not None:
            return get_http_client().cookies
        if cookies is None:
            return httpx.Cookies()
        return cast(httpx.Cookies, cookies)

    def get_upload_semaphore(self) -> asyncio.Semaphore:
        """Return the Sources-owned upload semaphore, creating it on first use.

        The semaphore caps the section that opens the source FD, registers the
        source, starts the resumable upload, and streams the body. Lazy
        construction keeps ``SourceUploadPipeline`` usable outside a running
        event loop. On post-finalize cancellation, the shielded finalize task
        may briefly keep an FD open after ``add_file`` exits the semaphore.
        """
        if self._upload_semaphore is None:
            self._upload_semaphore = asyncio.Semaphore(self._max_concurrent_uploads)
        return self._upload_semaphore

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
        upload_index: int = 0,
    ) -> Source:
        """Add a file source to a notebook using resumable upload."""
        # Audit C1: catch cross-loop add_file *before* touching
        # ``operation_scope`` or lazily allocating the upload semaphore.
        # Both are loop-bound on first use, so a cross-loop call would
        # otherwise attach a primitive to the wrong loop before the
        # documented ``RuntimeError`` guard fires (ADR-004).
        self._runtime.assert_bound_loop()
        module_logger.debug("Adding file source to notebook %s: %s", notebook_id, file_path)
        if title is not None:
            title = title.strip()
            if not title:
                raise ValidationError("Title cannot be empty or whitespace-only")

        # ``Path.resolve()`` / ``exists()`` / ``is_file()`` all hit the
        # filesystem (stat / readlink syscalls). On a slow network mount
        # or a deep symlink chain these are blocking calls — same problem
        # class as the ``open()`` + ``fstat()`` below — so they are
        # offloaded to a worker thread too.
        def _resolve_and_check(raw_path: str | Path) -> Path:
            resolved = Path(raw_path).resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"File not found: {resolved}")
            if not resolved.is_file():
                raise ValidationError(f"Not a regular file: {resolved}")
            return resolved

        file_path = await asyncio.to_thread(_resolve_and_check, file_path)

        filename = file_path.name
        content_type = _resolve_upload_content_type(file_path, mime_type)
        transient_error_types = _transient_error_types_for_upload(content_type)
        async with self._runtime.operation_scope(f"upload:{upload_index}"):
            upload_sem = self.get_upload_semaphore()
            upload_wait_start = monotonic()
            async with upload_sem:
                if self._record_upload_queue_wait is not None:
                    self._record_upload_queue_wait(monotonic() - upload_wait_start)

                # ``open()`` and ``fstat()`` are synchronous syscalls. For
                # network filesystems or deep directories they can block
                # the event loop for tens of milliseconds, stalling every
                # other concurrent task (auth refresh, sibling uploads,
                # the cancellation watchdog) for the duration of the
                # syscall. Run them on a worker thread so the loop keeps
                # ticking. ``fstat`` is paired with ``open`` in the same
                # closure so we don't pay the round-trip cost twice.
                def _open_and_stat(path: Path) -> tuple[IO[bytes], int]:
                    fh = open(path, "rb")  # noqa: SIM115
                    try:
                        size = os.fstat(fh.fileno()).st_size
                    except BaseException:
                        fh.close()
                        raise
                    return fh, size

                file_obj, file_size = await asyncio.to_thread(_open_and_stat, file_path)
                handed_off = False
                try:
                    source_id = await self.register_file_source(notebook_id, filename)
                    upload_url = await self.start_resumable_upload(
                        notebook_id,
                        filename,
                        file_size,
                        source_id,
                        content_type,
                    )
                    handed_off = True
                    await self.upload_file_streaming(
                        upload_url,
                        file_obj,
                        filename=filename,
                        on_progress=on_progress,
                        total_bytes=file_size,
                    )
                finally:
                    if not handed_off:
                        file_obj.close()

        needs_title_rename = title is not None and title != filename
        if wait:
            source = await self.wait_until_ready(
                notebook_id,
                source_id,
                timeout=wait_timeout,
                transient_error_types=transient_error_types,
            )
        elif needs_title_rename:
            source = await self.wait_until_registered(
                notebook_id,
                source_id,
                timeout=wait_timeout,
                transient_error_types=transient_error_types,
            )
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
                renamed = await self.rename(notebook_id, source_id, title)
                source = replace(source, title=renamed.title or title)
            except (RPCError, NetworkError):
                module_logger.warning(
                    "Source %s uploaded but rename to %r failed",
                    source_id,
                    title,
                    exc_info=True,
                )

        return source

    async def register_file_source(
        self,
        notebook_id: str,
        filename: str,
        *,
        list_sources: ListSources | None = None,
        logger: Any | None = None,
        get_source_limit: GetSourceLimit | None = None,
        rpc_call: RpcCallback | None = None,
    ) -> str:
        """Register a file source intent and get SOURCE_ID.

        Uses the same probe-then-create idempotency pattern as ``add_url`` /
        ``add_drive`` (P0-3-sources). The ADD_SOURCE_FILE RPC is mutating: a
        5xx / network failure between server-side commit and client-side
        response could otherwise duplicate the source on a naive retry.

        Probe semantics: unlike ``add_url`` (where URL equality is a stable
        dedupe key) or ``add_drive`` (where the Drive file_id is unique
        server-side), filenames are NOT identity-bearing — two distinct
        uploads of ``report.pdf`` are legitimately two separate sources.
        To avoid mis-matching a pre-existing source from an earlier upload,
        the probe captures a baseline of source IDs *before* the first
        create attempt and filters probe matches to IDs that are NOT in
        the baseline (the "new since the create started" set). An
        ambiguous match (>1 new source with the same filename, e.g. a
        concurrent uploader added one) raises ``SourceAddError`` rather
        than guessing.
        """
        params = [
            [[filename]],
            notebook_id,
            [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]
        if rpc_call is None:
            rpc_call = self._runtime.rpc_call
        if list_sources is None:
            list_sources = self.list_sources
        if logger is None:
            logger = module_logger
        if get_source_limit is None:
            get_source_limit = self._get_source_limit

        # Capture baseline source IDs before the first create attempt so the
        # probe can distinguish "this upload landed" from "a same-named source
        # already existed." Mirrors the pattern in NotebooksAPI.create.
        #
        # ``None`` is the "baseline unavailable" sentinel — used when the
        # baseline fetch failed (e.g. transient 5xx). The probe treats this
        # as "we cannot safely distinguish new sources from pre-existing
        # ones" and raises ``SourceAddError`` on any same-titled match,
        # rather than risk returning a pre-existing source as if it were the
        # just-created one. This protects against the silent
        # data-corruption mode where a failed create + pre-existing
        # same-name source would otherwise direct the subsequent upload
        # stream to the wrong source.
        baseline_ids: set[str] | None
        baseline_source_count: int | None
        try:
            baseline_sources = await list_sources(notebook_id)
            baseline_ids = {source.id for source in baseline_sources}
            baseline_source_count = len(baseline_sources)
        except Exception:
            logger.debug(
                "register_file_source: baseline list() failed; baseline unavailable",
                exc_info=True,
            )
            baseline_ids = None
            baseline_source_count = None

        async def _probe() -> str | None:
            try:
                sources = await list_sources(notebook_id)
            except (AuthError, RateLimitError, ServerError, NetworkError):
                # Transport- and auth-level probe failures must propagate
                # (P1-2) — otherwise idempotent_create would retry the
                # register on top of a broken probe.
                raise
            except Exception:
                logger.debug(
                    "register_file_source: probe list() failed with "
                    "non-transport error; treating as no match",
                    exc_info=True,
                )
                return None
            matches = [source for source in sources if source.title == filename]
            if baseline_ids is not None:
                matches = [source for source in matches if source.id not in baseline_ids]
            elif matches:
                # Baseline was unavailable so we cannot safely tell a new
                # source apart from a pre-existing one with the same name.
                # Surface this as an ambiguity rather than guessing — see
                # the ``baseline_ids`` comment above for the failure mode
                # this guards against.
                raise SourceAddError(
                    filename,
                    message=(
                        f"Cannot disambiguate file source with title {filename!r}: "
                        "baseline snapshot was unavailable, so a matching title may "
                        "predate this upload. Resolve manually before retrying."
                    ),
                )
            if len(matches) == 1:
                return matches[0].id
            if len(matches) > 1:
                raise SourceAddError(
                    filename,
                    message=(
                        f"Cannot disambiguate file source with title {filename!r}: "
                        f"probe found {len(matches)} new sources with this title "
                        "after a transport failure. Resolve manually before retrying."
                    ),
                )
            return None

        async def _create() -> str:
            try:
                result = await rpc_call(
                    RPCMethod.ADD_SOURCE_FILE,
                    params,
                    source_path=f"/notebook/{notebook_id}",
                    allow_null=False,
                    disable_internal_retries=True,
                )
            except (AuthError, RateLimitError, ServerError, NetworkError):
                # Transport-level signals must propagate so idempotent_create
                # can catch them and run the probe before retrying.
                raise
            except RPCError as exc:
                hint = ""
                if getattr(exc, "rpc_code", None) == _INVALID_ARGUMENT_RPC_CODE:
                    hint = await _build_invalid_argument_source_limit_hint(
                        source_count=baseline_source_count,
                        get_source_limit=get_source_limit,
                        logger=logger,
                    )
                raise SourceAddError(
                    filename,
                    cause=exc,
                    message=f"Failed to register file source for {filename}: {exc}{hint}",
                ) from exc

            source_id = _extract_register_file_source_id(result, filename)
            if source_id:
                return source_id

            # The RPC returned successfully but the response shape did not
            # contain a parseable SOURCE_ID. Before raising, run the probe
            # to see if the source landed server-side anyway — the
            # extraction can fail for schema-drift reasons (#474) while the
            # create has actually committed. This converts a recoverable
            # success into the same probe-recovery path that 5xx uses.
            probed_source_id = await _probe()
            if probed_source_id is not None:
                logger.info(
                    "register_file_source[%s]: response missing SOURCE_ID but "
                    "probe found a freshly committed source",
                    filename,
                )
                return probed_source_id

            if isinstance(result, str):
                preview = repr(result[:200])
                if len(result) > 200:
                    preview += "..."
            else:
                preview = repr(result)
                if len(preview) > 200:
                    preview = preview[:200] + "..."
            raise SourceAddError(
                filename,
                message=(
                    f"Failed to get SOURCE_ID from registration response. Response shape: {preview}"
                ),
            )

        return await idempotent_create(
            _create,
            _probe,
            label=f"sources.register_file_source[{filename}]",
        )

    async def list_sources(self, notebook_id: str) -> list[Source]:
        """List notebook sources for upload idempotency and polling."""
        return await self._lister.list(notebook_id)

    async def get_source(self, notebook_id: str, source_id: str) -> Source | None:
        """Get a source row by ID using the upload pipeline's lister."""
        return await self._lister.get(
            notebook_id,
            source_id,
            list_sources=self.list_sources,
        )

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
        """Wait for a source to become ready after upload."""
        return await self._poller.wait_until_ready(
            notebook_id,
            source_id,
            timeout=timeout,
            initial_interval=initial_interval,
            max_interval=max_interval,
            backoff_factor=backoff_factor,
            transient_error_types=transient_error_types,
            get_source=self.get_source,
            sleep=asyncio.sleep,
            monotonic=monotonic,
            logger=module_logger,
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
        """Wait until an uploaded source is registered server-side."""
        return await self._poller.wait_until_registered(
            notebook_id,
            source_id,
            timeout=timeout,
            initial_interval=initial_interval,
            max_interval=max_interval,
            backoff_factor=backoff_factor,
            transient_error_types=transient_error_types,
            get_source=self.get_source,
            sleep=asyncio.sleep,
            monotonic=monotonic,
            logger=module_logger,
        )

    async def rename(self, notebook_id: str, source_id: str, new_title: str) -> Source:
        """Rename an uploaded source."""
        module_logger.debug("Renaming source %s to: %s", source_id, new_title)
        params = [None, [source_id], [[[new_title]]]]
        result = await self._runtime.rpc_call(
            RPCMethod.UPDATE_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return Source.from_api_response(result) if result else Source(id=source_id, title=new_title)

    async def start_resumable_upload(
        self,
        notebook_id: str,
        filename: str,
        file_size: int,
        source_id: str,
        content_type: str,
    ) -> str:
        """Start a resumable upload session and get the upload URL."""
        auth_route = self._authuser_header()
        base_url = get_base_url()
        url = f"{get_upload_url()}?{self._authuser_query()}"

        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": base_url,
            "Referer": f"{base_url}/",
            "x-goog-authuser": auth_route,
            "x-goog-upload-command": "start",
            "x-goog-upload-header-content-length": str(file_size),
            "x-goog-upload-header-content-type": content_type,
            "x-goog-upload-protocol": "resumable",
        }

        body = json.dumps(
            {
                "PROJECT_ID": notebook_id,
                "SOURCE_NAME": filename,
                "SOURCE_ID": source_id,
            }
        )

        async with self._client_factory()(
            timeout=self._resolve_upload_timeout(httpx.Timeout(10.0, read=60.0)),
            cookies=self._live_cookies(),
        ) as client:
            response = await client.post(url, headers=headers, content=body)
            response.raise_for_status()

            upload_url = response.headers.get("x-goog-upload-url")
            if not upload_url:
                raise SourceAddError(
                    filename, message="Failed to get upload URL from response headers"
                )

            return upload_url

    async def upload_file_streaming(
        self,
        upload_url: str,
        file_obj: IO[bytes] | Path,
        *,
        filename: str | None = None,
        on_progress: Callable[[int, int], object] | None = None,
        total_bytes: int | None = None,
        logger: Any | None = None,
    ) -> None:
        """Stream upload file content to the resumable upload URL."""
        if logger is None:
            logger = module_logger
        path_fallback: Path | None = file_obj if isinstance(file_obj, Path) else None
        close_wired = False
        try:
            base_url = get_base_url()
            auth_route = self._authuser_header()
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

            async def file_stream():
                nonlocal uploaded_bytes
                if path_fallback is not None:
                    with open(path_fallback, "rb") as f:
                        while chunk := await asyncio.to_thread(f.read, 65536):
                            uploaded_bytes += len(chunk)
                            if on_progress is not None:
                                await maybe_await_callback(
                                    on_progress, uploaded_bytes, progress_total
                                )
                            yield chunk
                    return

                assert not isinstance(file_obj, Path)
                while chunk := await asyncio.to_thread(file_obj.read, 65536):
                    uploaded_bytes += len(chunk)
                    if on_progress is not None:
                        await maybe_await_callback(on_progress, uploaded_bytes, progress_total)
                    yield chunk

            finalize_started = False

            async def _do_finalize() -> None:
                nonlocal finalize_started
                async with self._client_factory()(
                    timeout=self._resolve_upload_timeout(httpx.Timeout(10.0, read=300.0)),
                    cookies=self._live_cookies(),
                ) as client:
                    finalize_started = True
                    response = await client.post(upload_url, headers=headers, content=file_stream())
                    response.raise_for_status()

            def _on_finalize_done(t: asyncio.Task[None]) -> None:
                if path_fallback is None:
                    try:
                        file_obj.close()  # type: ignore[union-attr]
                    except Exception as close_exc:  # noqa: BLE001
                        logger.debug("Caller FD close in finalize-done failed: %r", close_exc)
                if not t.cancelled() and (exc := t.exception()) is not None:
                    logger.debug("Background finalize POST failed: %r", exc)

            finalize_task = asyncio.create_task(_do_finalize())
            finalize_task.add_done_callback(_on_finalize_done)
            close_wired = True
            try:
                await asyncio.shield(finalize_task)
            except asyncio.CancelledError:
                if not finalize_started:
                    finalize_task.cancel()
                    _retain_background_cancel_task(
                        asyncio.create_task(
                            self.cancel_upload_session(
                                upload_url,
                                base_url,
                                auth_route,
                                logger=logger,
                            )
                        )
                    )
                    raise
                try:
                    await asyncio.shield(finalize_task)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Background finalize POST failed before cancellation propagated: %r",
                        exc,
                    )
                raise
        except BaseException:
            if not close_wired and path_fallback is None:
                try:
                    file_obj.close()  # type: ignore[union-attr]
                except Exception as close_exc:  # noqa: BLE001
                    logger.debug("Caller FD close on pre-wire exception failed: %r", close_exc)
            raise

    async def cancel_upload_session(
        self,
        upload_url: str,
        base_url: str,
        auth_route: str,
        *,
        logger: Any,
    ) -> None:
        """Best-effort POST a Scotty resumable-upload cancel command."""
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "x-goog-authuser": auth_route,
            "Origin": base_url,
            "Referer": f"{base_url}/",
            "x-goog-upload-command": "cancel",
        }
        try:
            async with self._client_factory()(
                timeout=httpx.Timeout(10.0, read=10.0),
                cookies=self._live_cookies(),
            ) as client:
                await client.post(upload_url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Best-effort Scotty cancel for %s failed: %r", upload_url, exc)


__all__ = [
    "RpcCallback",
    "SourceUploadPipeline",
    "UploadRuntime",
    "UploadRuntimeAdapter",
    "_SOURCE_ID_UUID_PATTERN",
    "_extract_register_file_source_id",
    "_looks_like_id_string",
]


if TYPE_CHECKING:
    # mypy guard: ``UploadRuntimeAdapter`` must structurally satisfy
    # the ``UploadRuntime`` Protocol it adapts. Failing this assertion
    # at type-check time catches shape drift on the delegate methods
    # before runtime construction at the composition root would. Mirrors
    # the pattern used by ``_rpc_executor._assert_rpc_executor_satisfies_rpc_caller``.

    def _assert_adapter_satisfies_upload_runtime(
        adapter: UploadRuntimeAdapter,
    ) -> None:
        _: UploadRuntime = adapter
