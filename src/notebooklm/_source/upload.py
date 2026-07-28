"""Private source file upload pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import IO, TYPE_CHECKING, Any, Protocol, cast

import httpx

from .._auth.account import authuser_query, format_authuser_value
from .._callbacks import maybe_await_callback
from .._env import get_base_url
from .._idempotency import idempotent_create
from .._loop_bound import LoopBoundPrimitive
from .._runtime.config import (
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    normalize_max_concurrent_uploads,
)
from .._runtime.contracts import (
    Kernel,
    RpcCaller,
)
from ..exceptions import (
    AuthError,
    NetworkError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from ..rpc import RPCError, RPCMethod, get_upload_url
from ..rpc.types import SourceStatus
from ..types import Source, SourceAddError

# Decode/validation helpers live in ``_upload_decode``; re-exported here so the
# historical ``notebooklm._source.upload.<helper>`` import surface (and the
# ``SourceUploadPipeline`` body below) keep resolving them. Note: each helper
# reads its module-level globals (e.g. ``urlsplit``, ``_SOURCE_ID_UUID_PATTERN``)
# from ``_upload_decode``, so a test seam that patches such a global must target
# ``_upload_decode`` — that is where the name is looked up — not this module.
from ._upload_decode import (  # noqa: F401
    _CONTEXTUAL_SOURCE_ID_FIELD_NAMES,
    _HTML_UPLOAD_CONTENT_TYPES,
    _HTML_UPLOAD_SUFFIXES,
    _MEDIA_APPLICATION_CONTENT_TYPES,
    _MEDIA_CONTENT_TYPE_PREFIXES,
    _MEDIA_TRANSIENT_ERROR_TYPES,
    _SOURCE_ID_ENVELOPE_MAX_DEPTH,
    _SOURCE_ID_FIELD_NAMES,
    _SOURCE_ID_UUID_PATTERN,
    _SOURCE_LIMIT_HINT_FLOOR,
    _SOURCE_NAME_FIELD_NAMES,
    _STRICT_TRANSIENT_ERROR_TYPES,
    _TIER_SOURCE_LIMITS_SUMMARY,
    GetSourceLimit,
    _build_invalid_argument_source_limit_hint,
    _coerce_filename_candidate,
    _coerce_source_id_candidate,
    _default_port_for_scheme,
    _extract_contextual_source_id_row_candidates,
    _extract_prefixed_singleton_source_id_envelope,
    _extract_register_file_source_id,
    _extract_singleton_source_id_envelope,
    _extract_source_id_field_candidates,
    _looks_like_id_string,
    _normalize_content_type,
    _normalize_upload_path,
    _redact_upload_url,
    _redacted_upload_authority,
    _register_response_shape_label,
    _resolve_upload_content_type,
    _source_context_names,
    _transient_error_types_for_upload,
    _unwrap_singleton_envelope,
    _validate_resumable_upload_url,
    _validate_upload_file_supported,
    raise_for_upload_status,
)
from .listing import SourceLister
from .polling import SourcePoller
from .upload_payloads import (
    build_register_file_source_params,
    build_rename_source_params,
    build_resumable_upload_start_request,
)

if TYPE_CHECKING:
    from .._runtime.lifecycle import ClientLifecycle
    from .._transport_drain import TransportDrainTracker


class AuthMetadata(Protocol):
    """Selected-account routing metadata required by upload flows.

    Inlined from ``_runtime.contracts`` (#1327): the upload pipeline is the only
    consumer, so this single-consumer Protocol lives local to its owner (ADR-0013
    ≥2-feature promotion bar). ``AuthTokens`` structurally satisfies it.
    """

    @property
    def authuser(self) -> int: ...

    @property
    def account_email(self) -> str | None: ...


class RpcCallback(Protocol):
    """RPC callback shape used by upload registration.

    A **callable** Protocol (``async def __call__(...)``) passed as a keyword arg
    into :meth:`SourceUploadPipeline.register_file_source` — distinct from the
    shared **object** Protocol ``RpcCaller`` (``.rpc_call(...)``); kept as a
    structural Protocol (not a ``Callable[...]`` alias) so mypy flags keyword-name
    typos at call sites.
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


_INVALID_ARGUMENT_RPC_CODE = 3
# Preserve the historical ``notebooklm._sources`` log channel after moving
# upload choreography into this module.
module_logger = logging.getLogger("notebooklm").getChild("_sources")


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


# Single-loop-per-client invariant per ADR-0004; not safe for multi-loop fan-out.
_BACKGROUND_CANCEL_TASKS: set[asyncio.Task[None]] = set()


def _retain_background_cancel_task(task: asyncio.Task[None]) -> None:
    _BACKGROUND_CANCEL_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_CANCEL_TASKS.discard)


class SourceUploadPipeline(LoopBoundPrimitive):
    """Own file registration and resumable upload orchestration."""

    def __init__(
        self,
        *,
        rpc: RpcCaller,
        drain: TransportDrainTracker,
        lifecycle: ClientLifecycle,
        kernel: Kernel,
        auth: AuthMetadata,
        upload_timeout: httpx.Timeout | None = None,
        max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
        record_upload_queue_wait: QueueWaitRecorder | None = None,
        async_client_factory: AsyncClientFactory | None = None,
        get_source_limit: GetSourceLimit | None = None,
        lister: SourceLister | None = None,
        poller: SourcePoller | None = None,
    ):
        self._rpc = rpc
        self._drain = drain
        self._lifecycle = lifecycle
        self._kernel = kernel
        self._auth = auth
        self._upload_timeout = upload_timeout
        self._record_upload_queue_wait = record_upload_queue_wait
        self._async_client_factory = async_client_factory
        self._max_concurrent_uploads = normalize_max_concurrent_uploads(max_concurrent_uploads)
        self._upload_semaphore: asyncio.Semaphore | None = None
        # Bounds concurrent Drive auto-route downloads (#1884); loop-bound.
        self._download_semaphore: asyncio.Semaphore | None = None
        # ``_bound_loop`` + ``set_bound_loop`` come from the
        # :class:`~notebooklm._loop_bound.LoopBoundPrimitive` base; this pipeline
        # overrides :meth:`_on_loop_rebind` to discard the cached
        # ``_upload_semaphore`` on a loop change. Cross-loop *use* is guarded by
        # the lifecycle's ``assert_bound_loop`` at the top of ``add_file``.
        # Defaults; SourcesAPI replaces these via configure_source_lifecycle()
        # so the pipeline shares its lister/poller (single owner for the
        # source-lifecycle verbs). Direct callers keep these fresh instances.
        self._lister = lister if lister is not None else SourceLister(self._rpc)
        self._poller = poller if poller is not None else SourcePoller()
        self._get_source_limit = get_source_limit

    def configure_source_limit_lookup(self, get_source_limit: GetSourceLimit | None) -> None:
        """Set the optional source-limit lookup used in registration hints."""
        self._get_source_limit = get_source_limit

    def configure_source_lifecycle(
        self,
        *,
        lister: SourceLister,
        poller: SourcePoller,
    ) -> None:
        """Adopt ``SourcesAPI``'s shared lister/poller as the single owner.

        Called from ``SourcesAPI.__init__`` so the pipeline's source-lifecycle
        verbs delegate to the SAME ``SourceLister`` / ``SourcePoller`` instances
        the public ``SourcesAPI`` uses, not parallel copies. Direct callers that
        never run through ``SourcesAPI`` keep the freshly-constructed defaults.
        """
        self._lister = lister
        self._poller = poller

    def _resolve_upload_timeout(self, default: httpx.Timeout) -> httpx.Timeout:
        """Return the configured upload timeout, or ``default`` if unset."""
        return self._upload_timeout if self._upload_timeout is not None else default

    def _client_factory(self) -> AsyncClientFactory:
        if self._async_client_factory is not None:
            return self._async_client_factory
        # Keep the upload leg on the SAME transport as the main session — the
        # /upload/ endpoint 500s on a curl_cffi-session + httpx-upload mix
        # (fingerprint/session correlation).
        from .._curl_cffi_transport import resolve_transport_factory

        return resolve_transport_factory()

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

    def live_cookies(self) -> httpx.Cookies:
        """Public accessor for the freshest live cookie jar (post-rotation, #1884).

        Exposes :meth:`_live_cookies` so ``SourcesAPI.add_drive_file`` can
        authenticate a SERVER-SIDE Drive download with the SAME ``.google.com``
        master jar the upload leg uses (kept fresh by keepalive rotation, unlike
        the on-disk cookies) — without reaching a private method across the seam.
        """
        return self._live_cookies()

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        """Discard the cached upload/download semaphores when the bound loop changes.

        Fires from :meth:`~notebooklm._loop_bound.LoopBoundPrimitive.set_bound_loop`
        only on a real loop change, so a stale semaphore bound to the old loop is
        never reused after a rebind (production also calls :meth:`reset_after_open`
        right after, making the discard idempotent). Cross-loop *use* is rejected
        by the lifecycle's ``assert_bound_loop``.
        """
        self._upload_semaphore = None
        self._download_semaphore = None

    def reset_after_open(self) -> None:
        """Discard the lazy upload semaphore so a reopened client rebinds it.

        Called from :meth:`ClientLifecycle.open` so a client closed and reopened
        on a *different* event loop builds a fresh ``asyncio.Semaphore`` on the new
        loop instead of reusing the stale one bound to the old (now-dead) loop
        (which on 3.10/3.11 can raise "bound to a different event loop" or mispark
        waiters). Mirrors ``ClientComposed.reset_after_open``; the semaphore is
        rebuilt lazily on the next :meth:`get_upload_semaphore` call.
        """
        self._upload_semaphore = None
        self._download_semaphore = None

    def get_upload_semaphore(self) -> asyncio.Semaphore:
        """Return the Sources-owned upload semaphore, creating it on first use.

        Caps the FD-open + register + resumable-upload + body-stream section. Lazy
        construction keeps the pipeline usable outside a running event loop.
        """
        if self._upload_semaphore is None:
            self._upload_semaphore = asyncio.Semaphore(self._max_concurrent_uploads)
        return self._upload_semaphore

    def get_download_semaphore(self) -> asyncio.Semaphore:
        """Return the Drive auto-route download semaphore (#1884), lazily built.

        A SEPARATE pool from the upload semaphore (gating the whole download→upload
        op can't deadlock against ``add_file``'s own slot). Asserts loop ownership
        FIRST (the download seam, as ``add_file`` is the upload seam) so a
        cross-loop ``add_drive_file`` fails before it touches this primitive (#1196).
        """
        self._lifecycle.assert_bound_loop()
        if self._download_semaphore is None:
            self._download_semaphore = asyncio.Semaphore(self._max_concurrent_uploads)
        return self._download_semaphore

    def authuser_value(self) -> str:
        """Account-routing value for Google URLs (#1884), matching the upload leg."""
        return format_authuser_value(self._auth.authuser, self._auth.account_email)

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
        """Add a file source to a notebook using resumable upload.

        Raises ``ValidationError`` for HTML-family uploads because
        NotebookLM's upload endpoint rejects those file extensions.
        """
        # Catch cross-loop add_file *before* touching
        # ``operation_scope`` or lazily allocating the upload semaphore.
        # Both are loop-bound on first use, so a cross-loop call would
        # otherwise attach a primitive to the wrong loop before the
        # documented ``RuntimeError`` guard fires (ADR-0004).
        self._lifecycle.assert_bound_loop()
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
        _validate_upload_file_supported(file_path, content_type)
        transient_error_types = _transient_error_types_for_upload(content_type)
        async with self._drain.operation_scope(f"upload:{upload_index}"):
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
                # ``renamed`` is ``None`` when the rename RPC echoes nothing;
                # fall back to the requested title (the source was just
                # uploaded, so it exists — only the echo is absent).
                source = replace(source, title=(renamed.title if renamed else None) or title)
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
        ``add_drive`` (the ADD_SOURCE_FILE RPC is mutating, so a 5xx/network
        failure between commit and response could duplicate on a naive retry).
        Because filenames are NOT identity-bearing (two uploads of ``report.pdf``
        are legitimately distinct), the probe captures a baseline of source IDs
        BEFORE the first create and only matches IDs new since then; an ambiguous
        match (>1 new source, e.g. a concurrent uploader) raises ``SourceAddError``
        rather than guessing.
        """
        params = build_register_file_source_params(filename, notebook_id)
        if rpc_call is None:
            rpc_call = self._rpc.rpc_call
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
                # — otherwise idempotent_create would retry the
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
                (match,) = matches  # exactly one (len==1 guard); unpack, not matches[0]
                return match.id
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
                if baseline_ids is None or source_id not in baseline_ids:
                    return source_id
                logger.info(
                    "register_file_source[%s]: response SOURCE_ID matched a "
                    "pre-existing source; probing for the newly registered source",
                    filename,
                )

            # The RPC returned successfully but the response shape did not
            # contain a trustworthy SOURCE_ID. Before raising, run the
            # source-list probe to see if the source landed server-side
            # anyway. This converts recoverable schema drift into the same
            # probe-recovery path that transport failures use without binding
            # unrelated ids from the response.
            try:
                probed_source_id = await _probe()
            except SourceAddError:
                raise
            except (AuthError, RateLimitError, ServerError, NetworkError) as exc:
                # The create RPC already returned successfully, so do not
                # let idempotent_create treat probe failure here as a
                # retryable create failure and re-POST the file source.
                raise SourceAddError(
                    filename,
                    cause=exc,
                    message=(
                        f"Cannot confirm registered file source for {filename!r}: "
                        "the register response did not provide a trustworthy "
                        f"SOURCE_ID and the source-list probe failed ({type(exc).__name__}). "
                        "Check the notebook source list before retrying."
                    ),
                ) from exc
            if probed_source_id is not None:
                logger.info(
                    "register_file_source[%s]: response missing SOURCE_ID but "
                    "probe found a freshly committed source",
                    filename,
                )
                return probed_source_id

            raise SourceAddError(
                filename,
                message=(
                    "Failed to get SOURCE_ID: no trustworthy SOURCE_ID found in "
                    f"{_register_response_shape_label(result)} registration response, "
                    "and the source-list probe found no "
                    "unambiguous new source. Check the notebook source list before retrying."
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

    async def rename(self, notebook_id: str, source_id: str, new_title: str) -> Source | None:
        """Rename a just-uploaded source, returning the ``UPDATE_SOURCE`` echo.

        Internal post-upload retitle helper. Returns the echoed
        :class:`~notebooklm.types.Source` when present, or ``None`` when the
        RPC echoes nothing — the caller (:meth:`add_file`) falls back to the
        requested title. Does not fabricate an unverified ``Source`` (the
        public ``sources.rename`` policy, issue #1255).
        """
        module_logger.debug("Renaming source %s to: %s", source_id, new_title)
        params = build_rename_source_params(source_id, new_title)
        result = await self._rpc.rpc_call(
            RPCMethod.UPDATE_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        if result:
            return Source.from_api_response(result, method_id=RPCMethod.UPDATE_SOURCE.value)
        return None

    async def start_resumable_upload(
        self,
        notebook_id: str,
        filename: str,
        file_size: int,
        source_id: str,
        content_type: str,
    ) -> str:
        """Start a resumable upload session and get the upload URL."""
        request = build_resumable_upload_start_request(
            notebook_id=notebook_id,
            filename=filename,
            file_size=file_size,
            source_id=source_id,
            content_type=content_type,
            base_url=get_base_url(),
            upload_url=get_upload_url(),
            authuser_query=self._authuser_query(),
            authuser_header=self._authuser_header(),
        )

        async with self._client_factory()(
            timeout=self._resolve_upload_timeout(httpx.Timeout(10.0, read=60.0)),
            cookies=self._live_cookies(),
        ) as client:
            response = await client.post(
                request.url,
                headers=request.headers,
                content=request.body,
            )
            # Classify a rejection (e.g. an unsupported ``.pub`` → HTTP 400) instead of
            # leaking a raw ``httpx.HTTPStatusError`` to callers (#1892).
            raise_for_upload_status(response, filename)

            upload_url = response.headers.get("x-goog-upload-url")
            if not upload_url:
                raise SourceAddError(
                    filename, message="Failed to get upload URL from response headers"
                )

            try:
                return _validate_resumable_upload_url(upload_url)
            except ValidationError as exc:
                raise SourceAddError(
                    filename,
                    cause=exc,
                    message=f"Received invalid resumable upload URL from NotebookLM: {exc}",
                ) from exc

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
            upload_url = _validate_resumable_upload_url(upload_url)
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
            logger.debug("Streaming upload to %s for %s", _redact_upload_url(upload_url), diag_name)
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
                    # The curl_cffi transport streams the request body from disk via
                    # low-level libcurl (no full-file buffer); httpx streams natively
                    # through the async-generator ``content=`` path. isinstance (not
                    # duck-typing) because test mocks auto-spawn any attribute.
                    from .._curl_cffi_transport import CurlCffiAsyncClient

                    if isinstance(client, CurlCffiAsyncClient) and total_bytes is not None:
                        source = path_fallback if path_fallback is not None else file_obj
                        response = await client.stream_upload(
                            upload_url, source, total_bytes=total_bytes, headers=headers
                        )
                        if on_progress is not None:
                            await maybe_await_callback(on_progress, progress_total, progress_total)
                    else:
                        response = await client.post(
                            upload_url, headers=headers, content=file_stream()
                        )
                    # The finalize POST can also be rejected upstream (propagates via
                    # ``asyncio.shield`` below); classify it too (#1892).
                    raise_for_upload_status(response, diag_name)

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
            upload_url = _validate_resumable_upload_url(upload_url)
            async with self._client_factory()(
                timeout=httpx.Timeout(10.0, read=10.0),
                cookies=self._live_cookies(),
            ) as client:
                await client.post(upload_url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Best-effort Scotty cancel for %s failed: %r",
                _redact_upload_url(upload_url),
                exc,
            )


__all__ = [
    "RpcCallback",
    "SourceUploadPipeline",
    "_SOURCE_ID_UUID_PATTERN",
    "_extract_register_file_source_id",
    "_looks_like_id_string",
]
