"""Private source file upload pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import IO, TYPE_CHECKING, Any, Protocol

import httpx

from ..._auth.account import authuser_query, format_authuser_value
from ..._callbacks import maybe_await_callback
from ..._http_client_factory import HttpClientFactories
from ..._idempotency import (
    OperationJournal,
    attach_journal_entry,
    attach_reconciliation_report,
    bind_operation_journal_entries,
    call_unconfirmed_on_transport_loss,
    mark_commit_state,
    reconciliation_report,
)
from ..._idempotency import mark_unconfirmed as _unconfirmed
from ..._loop_bound import EpochFenced
from ..._request_context import request_policy_scope
from ..._request_policy import RequestPolicyOwner, request_scoped
from ..._runtime.config import (
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    normalize_max_concurrent_uploads,
)
from ..._source.polling import SourcePoller
from ...exceptions import (
    AuthError,
    NetworkError,
    NotebookLMError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from ...outcomes import CommitState
from ...rpc import RPCError, RPCMethod, get_upload_url
from ...types import Source, SourceAddError
from ..contracts import (
    Kernel,
    RpcCaller,
)
from ..params.sources import (
    build_register_file_source_params,
    build_rename_source_params,
    build_resumable_upload_start_request,
)
from ..rows.source_models import decode_source

# Decode/validation helpers live in ``_upload_decode``; re-exported here so the
# historical ``notebooklm._web.sources.upload.<helper>`` import surface (and the
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
    SourceAddStage,
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
    _upload_url_origin,
    _validate_resumable_upload_url,
    _validate_upload_file_supported,
    raise_for_upload_status,
    raise_partial_upload_failure,
)
from .listing import SourceLister

if TYPE_CHECKING:
    from ..._runtime.call_supervisor import CallSupervisor
    from ..._sources import _UploadedSourceFinalizer


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


@dataclass(frozen=True)
class _TransportChildOutcome:
    """Internal child result that keeps process exits inside the task boundary."""

    error: BaseException | None = None


class SourceUploadPipeline(RequestPolicyOwner, EpochFenced):
    """Own file registration and resumable upload orchestration."""

    name = "web-upload"

    def __init__(
        self,
        *,
        rpc: RpcCaller,
        supervisor: CallSupervisor,
        kernel: Kernel,
        auth: AuthMetadata,
        upload_timeout: httpx.Timeout | None = None,
        start_timeout: httpx.Timeout | None = None,
        finalize_timeout: httpx.Timeout | None = None,
        drive_timeout: httpx.Timeout | None = None,
        max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
        record_upload_queue_wait: QueueWaitRecorder | None = None,
        async_client_factory: AsyncClientFactory | None = None,
        http_client_factories: HttpClientFactories | None = None,
        get_source_limit: GetSourceLimit | None = None,
        lister: SourceLister | None = None,
        poller: SourcePoller | None = None,
    ):
        super().__init__("NotebookLMClient upload generation is retired")
        self._rpc = rpc
        self._supervisor = supervisor
        self._kernel = kernel
        self._auth = auth
        self._start_timeout = start_timeout if start_timeout is not None else upload_timeout
        self._finalize_timeout = (
            finalize_timeout if finalize_timeout is not None else upload_timeout
        )
        self._drive_timeout = drive_timeout
        self._record_upload_queue_wait = record_upload_queue_wait
        self._async_client_factory = async_client_factory
        self._http_client_factories = http_client_factories
        self._max_concurrent_uploads = normalize_max_concurrent_uploads(max_concurrent_uploads)
        self._upload_semaphore: asyncio.Semaphore | None = None
        # Bounds concurrent Drive auto-route downloads (#1884); loop-bound.
        self._download_semaphore: asyncio.Semaphore | None = None
        self._registry_lock: asyncio.Lock | None = None
        self._transport_tasks: set[asyncio.Task[Any]] = set()
        self._transport_clients: set[httpx.AsyncClient] = set()
        # ``_bound_loop`` + ``set_bound_loop`` come from the
        # :class:`~notebooklm._loop_bound.LoopBoundPrimitive` base; this pipeline
        # overrides :meth:`_on_loop_rebind` to discard the cached
        # ``_upload_semaphore`` on a loop change. Cross-loop *use* is guarded by
        # the lifecycle's ``assert_bound_loop`` at the top of ``add_file``.
        # Defaults; WebSourcesAPI replaces these via configure_source_lifecycle()
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
        """Adopt ``WebSourcesAPI``'s shared lister/poller as the single owner.

        Called from ``WebSourcesAPI.__init__`` so the pipeline's source-lifecycle
        verbs delegate to the SAME ``SourceLister`` / ``SourcePoller`` instances
        the public ``WebSourcesAPI`` uses, not parallel copies. Direct callers that
        never run through ``WebSourcesAPI`` keep the freshly-constructed defaults.
        """
        self._lister = lister
        self._poller = poller

    @staticmethod
    def _resolve_upload_timeout(
        configured: httpx.Timeout | None, default: httpx.Timeout
    ) -> httpx.Timeout:
        """Return one phase's configured timeout, or its backend default."""
        return configured if configured is not None else default

    @request_scoped
    def _client_factory(self) -> AsyncClientFactory:
        if self._async_client_factory is not None:
            return self._async_client_factory
        # Keep the upload leg on the SAME transport as the main session — the
        # /upload/ endpoint 500s on a curl_cffi-session + httpx-upload mix
        # (fingerprint/session correlation).
        from ..._curl_cffi_transport import resolve_transport_factory

        factory = resolve_transport_factory()
        if self._http_client_factories is not None:
            return self._http_client_factories.select(factory)
        return factory

    def _authuser_query(self) -> str:
        return authuser_query(self._auth.authuser, self._auth.account_email)

    def _authuser_header(self) -> str:
        return format_authuser_value(self._auth.authuser, self._auth.account_email)

    def _live_cookies(self, expected_epoch: int) -> httpx.Cookies:
        """Return cookies only from the HTTP client owning ``expected_epoch``."""
        self.assert_epoch(expected_epoch)
        return self._kernel.get_http_client(expected_epoch=expected_epoch).cookies

    def live_cookies(self, expected_epoch: int) -> httpx.Cookies:
        """Public accessor for the freshest live cookie jar (post-rotation, #1884).

        Exposes :meth:`_live_cookies` so ``SourcesAPI.add_drive_file`` can
        authenticate a SERVER-SIDE Drive download with the SAME ``.google.com``
        master jar the upload leg uses (kept fresh by keepalive rotation, unlike
        the on-disk cookies) — without reaching a private method across the seam.
        """
        return self._live_cookies(expected_epoch)

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
        self._registry_lock = None

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

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        """Bind a lazy upload generation without issuing network I/O."""
        self.set_bound_loop(loop)
        self.reset_after_open()
        self.activate(epoch)
        self._registry_lock = asyncio.Lock()
        self._transport_tasks.clear()
        self._transport_clients.clear()

    async def prepare_close(self) -> None:
        """Fence first, then interrupt every old-epoch upload resource."""
        self.fence()
        tasks, clients = await self._snapshot_transport_resources()
        error = await self._settle_transport_resources(tasks, clients)
        if error is not None:
            raise error

    async def close_resources(self) -> None:
        """Settle and clear all direct-upload handles after partial open/close."""
        # ``prepare_close`` normally installed this fence.  Repeat it before
        # the first await so rollback/partial-open cleanup is independently
        # safe and cannot race a late child registration.
        self.fence()
        try:
            tasks, clients = await self._snapshot_transport_resources()
            error = await self._settle_transport_resources(tasks, clients)
            if error is not None:
                raise error
        finally:
            self._transport_clients.clear()
            self._transport_tasks.clear()
            self.fence()
            self._registry_lock = None

    async def _snapshot_transport_resources(
        self,
    ) -> tuple[list[asyncio.Task[Any]], list[httpx.AsyncClient]]:
        """Snapshot every old-generation resource after the epoch fence lands."""
        lock = self._registry_lock
        if lock is None:
            return [], []
        current = asyncio.current_task()
        async with lock:
            tasks = [task for task in self._transport_tasks if task is not current]
            clients = list(self._transport_clients)
        return tasks, clients

    async def _settle_transport_resources(
        self,
        tasks: list[asyncio.Task[Any]],
        clients: list[httpx.AsyncClient],
    ) -> BaseException | None:
        """Cancel/gather every task and attempt every client close.

        Process-exit signals retain precedence, but no individual teardown
        failure is allowed to skip a sibling task, request body, or client.
        """
        for task in tasks:
            if not task.done():
                task.cancel()
        client_error = await self._close_clients(clients)
        task_results = await asyncio.gather(*tasks, return_exceptions=True)
        task_errors = [
            result.error if isinstance(result, _TransportChildOutcome) else result
            for result in task_results
        ]
        process_exit = next(
            (
                result
                for result in (*task_errors, client_error)
                if isinstance(result, (KeyboardInterrupt, SystemExit))
            ),
            None,
        )
        if process_exit is not None:
            return process_exit
        if client_error is not None:
            return client_error
        return next(
            (
                result
                for result in task_errors
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            ),
            None,
        )

    @staticmethod
    async def _close_clients(
        clients: list[httpx.AsyncClient],
    ) -> BaseException | None:
        """Attempt every client close without leaking process exits via child tasks."""
        process_exit: KeyboardInterrupt | SystemExit | None = None
        first_failure: BaseException | None = None
        for client in clients:
            try:
                await client.aclose()
            except (KeyboardInterrupt, SystemExit) as exc:
                if process_exit is None:
                    process_exit = exc
            except BaseException as exc:
                if first_failure is None:
                    first_failure = exc
        return process_exit or first_failure

    def _begin_transport_operation(
        self,
        expected_epoch: int,
    ) -> tuple[int, asyncio.Task[Any]]:
        epoch = expected_epoch
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("NotebookLMClient upload transport is not open.")
        self.assert_epoch(epoch)
        self._transport_tasks.add(task)
        return epoch, task

    def _finish_transport_operation(self, epoch: int, task: asyncio.Task[Any]) -> None:
        del epoch
        self._transport_tasks.discard(task)

    @asynccontextmanager
    async def transport_operation_scope(self, label: str) -> AsyncIterator[int]:
        """Admit and track one direct-upload workflow under one epoch."""
        with request_policy_scope(self.request_policy):
            self._supervisor.assert_bound_loop()
            async with self._supervisor.operation_scope(label) as lease:
                epoch, task = self._begin_transport_operation(lease.epoch)
                try:
                    yield lease.epoch
                finally:
                    self._finish_transport_operation(epoch, task)

    def _track_transport_client(self, client: httpx.AsyncClient, epoch: int) -> None:
        """Publish a new client in one checkpoint-free fencing section."""
        self.assert_epoch(epoch)
        if self._registry_lock is None:
            raise RuntimeError("NotebookLMClient upload transport is not open.")
        self._transport_clients.add(client)

    async def _spawn_transport_child(
        self,
        label: str,
        factory: Callable[[], Awaitable[Any]],
        *,
        expected_epoch: int,
    ) -> asyncio.Task[_TransportChildOutcome]:
        """Spawn child I/O whose task is registered before its first await.

        Registration happens inside the admitted child factory.  Therefore a
        parent cancelled in the tiny interval before ``spawn_child`` publishes
        its return value cannot lose the task from uploader teardown.
        """

        async def _tracked() -> _TransportChildOutcome:
            task = asyncio.current_task()
            try:
                self.assert_epoch(expected_epoch)
                if task is None:
                    raise RuntimeError("NotebookLMClient upload child has no owning task.")
                if self._registry_lock is None:
                    raise RuntimeError("NotebookLMClient upload transport is not open.")
                self._transport_tasks.add(task)
                await factory()
                return _TransportChildOutcome()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                return _TransportChildOutcome(exc)
            finally:
                if task is not None:
                    self._transport_tasks.discard(task)

        return await self._supervisor.spawn_child(label, _tracked)

    def get_upload_semaphore(self) -> asyncio.Semaphore:
        """Return the Sources-owned upload semaphore, creating it on first use.

        Caps the FD-open + register + resumable-upload + body-stream section. Lazy
        construction keeps the pipeline usable outside a running event loop.
        """
        if self._upload_semaphore is None:
            self._upload_semaphore = asyncio.Semaphore(self._max_concurrent_uploads)
        return self._upload_semaphore

    @asynccontextmanager
    async def _upload_slot(self) -> AsyncIterator[None]:
        upload_sem = self.get_upload_semaphore()
        upload_wait_start = monotonic()
        async with upload_sem:
            if self._record_upload_queue_wait is not None:
                self._record_upload_queue_wait(monotonic() - upload_wait_start)
            yield

    def get_download_semaphore(self) -> asyncio.Semaphore:
        """Return the Drive auto-route download semaphore (#1884), lazily built.

        A SEPARATE pool from the upload semaphore (gating the whole download→upload
        op can't deadlock against ``add_file``'s own slot). Asserts loop ownership
        FIRST (the download seam, as ``add_file`` is the upload seam) so a
        cross-loop ``add_drive_file`` fails before it touches this primitive (#1196).
        """
        self._supervisor.assert_bound_loop()
        if self._download_semaphore is None:
            self._download_semaphore = asyncio.Semaphore(self._max_concurrent_uploads)
        return self._download_semaphore

    def authuser_value(self) -> str:
        """Account-routing value for Google URLs (#1884), matching the upload leg."""
        return format_authuser_value(self._auth.authuser, self._auth.account_email)

    @asynccontextmanager
    async def drive_download_scope(
        self,
        document_id: str,
    ) -> AsyncIterator[tuple[Path, str, str | None]]:
        """Yield one authenticated Drive download for a backend upload adapter.

        This is the narrow cross-backend seam for the public ``add_drive_file``
        convenience operation.  It owns account routing, live-cookie access,
        download admission, Drive reference validation, and guaranteed temp-file
        cleanup; consumers receive only a path, display filename, and MIME type.
        """

        from .drive_import import DriveFetcher, parse_drive_ref

        async with (
            self.transport_operation_scope("drive-download") as epoch,
            self.get_download_semaphore(),
        ):
            download = await DriveFetcher(
                cookies_provider=lambda: self.live_cookies(epoch),
                authuser=self.authuser_value(),
                timeout=self._drive_timeout,
                http_client_factories=self._http_client_factories,
            )(parse_drive_ref(document_id))
            try:
                yield download.path, download.filename, download.content_type
            finally:
                download.path.unlink(missing_ok=True)

    @request_scoped
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
        finalize_uploaded: _UploadedSourceFinalizer,
    ) -> Source:
        """Add a file while holding admission through reconciliation and rename."""
        # Pure argument/MIME rejection stays outside admission. In particular,
        # a drained client must still report an unsupported HTML upload as an
        # input error. The filesystem-backed resolve/stat remains inside the
        # full workflow scope below so close waits for that awaited work.
        raw_path = Path(file_path)
        content_type = _resolve_upload_content_type(raw_path, mime_type)
        _validate_upload_file_supported(raw_path, content_type)
        # Assert affinity before entering the supervisor's loop-bound scope.
        async with self.transport_operation_scope(f"upload:{upload_index}") as epoch:
            return await self._add_file_admitted(
                notebook_id,
                raw_path,
                wait,
                wait_timeout,
                title=title,
                on_progress=on_progress,
                upload_index=upload_index,
                _mime_type=mime_type,
                _expected_epoch=epoch,
                finalize_uploaded=finalize_uploaded,
            )

    async def _add_file_admitted(
        self,
        notebook_id: str,
        file_path: str | Path,
        wait: bool = False,
        wait_timeout: float = 120.0,
        *,
        title: str | None = None,
        on_progress: Callable[[int, int], object] | None = None,
        upload_index: int = 0,
        _mime_type: str | None,
        _expected_epoch: int,
        finalize_uploaded: _UploadedSourceFinalizer,
    ) -> Source:
        """Add a file source to a notebook using resumable upload.

        Raises ``ValidationError`` for HTML-family uploads because
        NotebookLM's upload endpoint rejects those file extensions.
        """
        module_logger.debug("Adding file source to notebook %s: %s", notebook_id, file_path)

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
        # Re-resolve against the canonical target to preserve historical
        # symlink behavior; the equivalent pure check already ran before
        # admission, while a target whose suffix differs is necessarily known
        # only after the awaited filesystem resolution.
        content_type = _resolve_upload_content_type(file_path, _mime_type)
        _validate_upload_file_supported(file_path, content_type)
        transient_error_types = _transient_error_types_for_upload(content_type)
        async with self._upload_slot():
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
                registration = await self._register_file_source_for_upload(notebook_id, filename)
                source_id = registration
                stage: SourceAddStage = "start_session"
                try:
                    upload_url = await self.start_resumable_upload(
                        notebook_id,
                        filename,
                        file_size,
                        source_id,
                        content_type,
                        expected_epoch=_expected_epoch,
                    )
                    stage = "upload_finalize"
                    handed_off = True
                    await self.upload_file_streaming(
                        upload_url,
                        file_obj,
                        filename=filename,
                        on_progress=on_progress,
                        total_bytes=file_size,
                        expected_epoch=_expected_epoch,
                    )
                except Exception as exc:  # noqa: BLE001 - preserve post-register failures
                    raise_partial_upload_failure(exc, filename, source_id=source_id, stage=stage)
            finally:
                if not handed_off:
                    file_obj.close()

        async def _wait_until_ready(
            target_notebook_id: str,
            target_source_id: str,
            timeout: float,
        ) -> Source:
            return await self.wait_until_ready(
                target_notebook_id,
                target_source_id,
                timeout=timeout,
                transient_error_types=transient_error_types,
            )

        async def _wait_until_registered(
            target_notebook_id: str,
            target_source_id: str,
            timeout: float,
        ) -> Source:
            return await self.wait_until_registered(
                target_notebook_id,
                target_source_id,
                timeout=timeout,
                transient_error_types=transient_error_types,
            )

        async def _rename_uploaded(
            target_notebook_id: str,
            target_source_id: str,
            requested_title: str,
        ) -> str | None:
            renamed = await self.rename(
                target_notebook_id,
                target_source_id,
                requested_title,
            )
            return renamed.title if renamed is not None else None

        return await finalize_uploaded(
            notebook_id,
            source_id,
            filename,
            wait=wait,
            wait_timeout=wait_timeout,
            title=title,
            wait_until_ready=_wait_until_ready,
            wait_until_registered=_wait_until_registered,
            rename_uploaded=_rename_uploaded,
        )

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
        """Register a file source exactly once and return its correlated source ID."""
        return await self._register_file_source_result(
            notebook_id,
            filename,
            list_sources=list_sources,
            logger=logger,
            get_source_limit=get_source_limit,
            rpc_call=rpc_call,
        )

    async def _register_file_source_for_upload(self, notebook_id: str, filename: str) -> str:
        """Normalize built-in and legacy registration seams for add_file."""
        register = self.register_file_source
        if getattr(register, "__func__", None) is SourceUploadPipeline.register_file_source:
            return await self._register_file_source_result(notebook_id, filename)
        # Preserve injected and overridden legacy seams that only accept the
        # historical (notebook_id, filename) call shape.
        return await register(notebook_id, filename)

    async def _register_file_source_result(
        self,
        notebook_id: str,
        filename: str,
        *,
        list_sources: ListSources | None = None,
        logger: Any | None = None,
        get_source_limit: GetSourceLimit | None = None,
        rpc_call: RpcCallback | None = None,
    ) -> str:
        """Register once; candidate inspection after uncertainty never recovers."""
        params = build_register_file_source_params(filename, notebook_id)
        rpc_call = rpc_call or self._rpc.rpc_call
        list_sources = list_sources or self.list_sources
        logger = logger or module_logger
        get_source_limit = get_source_limit or self._get_source_limit
        journal = OperationJournal("sources.add_file")
        journal_entry = journal.new_entry(method=RPCMethod.ADD_SOURCE_FILE.value)

        async def _inspect_candidates(exc: NotebookLMError) -> None:
            try:
                sources = await list_sources(notebook_id)
            except Exception:
                logger.warning(
                    "register_file_source: candidate inspection failed; preserving "
                    "the original registration error",
                    exc_info=True,
                )
                return
            matching_ids = [source.id for source in sources if source.title == filename]
            attach_reconciliation_report(
                exc,
                reconciliation_report(
                    matching_ids,
                    [filename],
                    reason="file registration response did not correlate a source id",
                ),
                operation="sources.add_file",
            )

        async def _create() -> str:
            try:
                with bind_operation_journal_entries(journal_entry):
                    result = await rpc_call(
                        RPCMethod.ADD_SOURCE_FILE,
                        params,
                        source_path=f"/notebook/{notebook_id}",
                        allow_null=False,
                        disable_internal_retries=True,
                    )
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as exc:
                hint = ""
                if getattr(exc, "rpc_code", None) == _INVALID_ARGUMENT_RPC_CODE:
                    try:
                        source_count = len(await list_sources(notebook_id))
                    except Exception:
                        source_count = None
                    hint = await _build_invalid_argument_source_limit_hint(
                        source_count=source_count,
                        get_source_limit=get_source_limit,
                        logger=logger,
                    )
                error = SourceAddError(
                    filename,
                    cause=exc,
                    message=f"Failed to register file source for {filename}: {exc}{hint}",
                )
                state = getattr(exc, "commit_state", None)
                if state is not None:
                    mark_commit_state(error, state, operation="sources.add_file")
                if getattr(exc, "unconfirmed", False):
                    _unconfirmed(error, operation="sources.add_file")
                attach_journal_entry(error, journal_entry)
                raise error from exc

            source_id = _extract_register_file_source_id(result, filename)
            if source_id:
                journal_entry.record(
                    CommitState.CONFIRMED,
                    "decoded file registration",
                    known_resource_ids=(source_id,),
                )
                return source_id

            error = _unconfirmed(
                SourceAddError(
                    filename,
                    message=(
                        "Failed to get SOURCE_ID: no trustworthy SOURCE_ID found in "
                        f"{_register_response_shape_label(result)} registration response. "
                        "Check the notebook source list before retrying."
                    ),
                ),
                operation="sources.add_file",
            )
            await _inspect_candidates(error)
            raise attach_journal_entry(error, journal_entry)

        try:
            return await call_unconfirmed_on_transport_loss(
                _create,
                method=RPCMethod.ADD_SOURCE_FILE,
                what="the file-source registration",
                operation="sources.add_file",
                journal_entry=journal_entry,
            )
        except NotebookLMError as exc:
            metadata = exc.operation_metadata
            if getattr(exc, "unconfirmed", False) and (
                metadata is None or metadata.reconciliation is None
            ):
                await _inspect_candidates(exc)
            raise

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
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
        )
        if result:
            return decode_source(Source, result, method_id=RPCMethod.UPDATE_SOURCE.value)
        return None

    @request_scoped
    async def start_resumable_upload(
        self,
        notebook_id: str,
        filename: str,
        file_size: int,
        source_id: str,
        content_type: str,
        *,
        expected_epoch: int,
    ) -> str:
        """Start a resumable upload session and get the upload URL."""
        self.assert_epoch(expected_epoch)
        request = build_resumable_upload_start_request(
            notebook_id=notebook_id,
            filename=filename,
            file_size=file_size,
            source_id=source_id,
            content_type=content_type,
            upload_url=get_upload_url(),
            authuser_query=self._authuser_query(),
            authuser_header=self._authuser_header(),
        )

        cookies = self._live_cookies(expected_epoch)
        self.assert_epoch(expected_epoch)
        client = self._client_factory()(
            timeout=self._resolve_upload_timeout(
                self._start_timeout,
                httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
            ),
            cookies=cookies,
        )
        self._track_transport_client(client, expected_epoch)
        try:
            async with client:
                self.assert_epoch(expected_epoch)
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
        finally:
            self._transport_clients.discard(client)

    @request_scoped
    async def upload_file_streaming(
        self,
        upload_url: str,
        file_obj: IO[bytes] | Path,
        *,
        filename: str | None = None,
        on_progress: Callable[[int, int], object] | None = None,
        total_bytes: int | None = None,
        logger: Any | None = None,
        expected_epoch: int,
    ) -> None:
        """Stream upload file content to the resumable upload URL."""
        self.assert_epoch(expected_epoch)
        if logger is None:
            logger = module_logger
        path_fallback: Path | None = file_obj if isinstance(file_obj, Path) else None
        close_wired = False
        try:
            upload_url = _validate_resumable_upload_url(upload_url)
            # Origin/Referer track the *validated* upload URL, never the configured
            # base URL: the two personal hosts stand in for each other, and an
            # Origin naming the other host fails Google's origin-bound auth checks.
            origin = _upload_url_origin(upload_url)
            auth_route = self._authuser_header()
            headers = {
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                "x-goog-authuser": auth_route,
                "Origin": origin,
                "Referer": f"{origin}/",
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
                try:
                    cookies = self._live_cookies(expected_epoch)
                    self.assert_epoch(expected_epoch)
                    client = self._client_factory()(
                        timeout=self._resolve_upload_timeout(
                            self._finalize_timeout,
                            httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
                        ),
                        cookies=cookies,
                    )
                    self._track_transport_client(client, expected_epoch)
                    try:
                        async with client:
                            self.assert_epoch(expected_epoch)
                            finalize_started = True
                            # The curl_cffi transport streams the request body from disk via
                            # low-level libcurl (no full-file buffer); httpx streams natively
                            # through the async-generator ``content=`` path. isinstance (not
                            # duck-typing) because test mocks auto-spawn any attribute.
                            from ..._curl_cffi_transport import CurlCffiAsyncClient

                            if isinstance(client, CurlCffiAsyncClient) and total_bytes is not None:
                                source = path_fallback if path_fallback is not None else file_obj
                                response = await client.stream_upload(
                                    upload_url, source, total_bytes=total_bytes, headers=headers
                                )
                                if on_progress is not None:
                                    await maybe_await_callback(
                                        on_progress, progress_total, progress_total
                                    )
                            else:
                                response = await client.post(
                                    upload_url, headers=headers, content=file_stream()
                                )
                            # The finalize POST can also be rejected upstream (propagates via
                            # ``asyncio.shield`` below); classify it too (#1892).
                            raise_for_upload_status(response, diag_name)
                    finally:
                        self._transport_clients.discard(client)
                finally:
                    # A caller-owned file object is the request body resource.
                    # Close it *inside* the tracked task so gathering the task
                    # proves teardown complete; a done callback is too late for
                    # a close/reopen boundary.
                    if path_fallback is None:
                        try:
                            file_obj.close()  # type: ignore[union-attr]
                        except Exception as close_exc:  # noqa: BLE001
                            logger.debug("Caller FD close in finalize failed: %r", close_exc)

            def _on_finalize_done(t: asyncio.Task[_TransportChildOutcome]) -> None:
                # ``CallSupervisor.spawn_child`` admits the wrapper before it
                # publishes the task.  A caller may cancel in the narrow gap
                # before ``_do_finalize`` reaches its own ``finally``; keep an
                # idempotent fallback for that no-first-step boundary.
                if path_fallback is None:
                    try:
                        file_obj.close()  # type: ignore[union-attr]
                    except Exception as close_exc:  # noqa: BLE001
                        logger.debug("Caller FD close in finalize-done failed: %r", close_exc)
                if not t.cancelled():
                    outcome = t.result()
                    if outcome.error is not None:
                        logger.debug("Background finalize POST failed: %r", outcome.error)

            finalize_task = await self._spawn_transport_child(
                f"upload-finalize:{diag_name}",
                _do_finalize,
                expected_epoch=expected_epoch,
            )
            finalize_task.add_done_callback(_on_finalize_done)
            close_wired = True
            try:
                outcome = await asyncio.shield(finalize_task)
                if outcome.error is not None:
                    raise outcome.error
            except asyncio.CancelledError as cancelled:
                if not finalize_started:
                    finalize_task.cancel()
                    await asyncio.gather(finalize_task, return_exceptions=True)
                    cancel_task: asyncio.Task[Any] | None = None
                    try:
                        cancel_task = await self._spawn_transport_child(
                            f"upload-cancel:{diag_name}",
                            lambda: self.cancel_upload_session(
                                upload_url,
                                auth_route,
                                logger=logger,
                                _expected_epoch=expected_epoch,
                            ),
                            expected_epoch=expected_epoch,
                        )
                        cancel_outcome = await asyncio.shield(cancel_task)
                        if cancel_outcome.error is not None:
                            raise cancel_outcome.error
                    except asyncio.CancelledError:
                        if cancel_task is not None:
                            cancel_task.cancel()
                            await asyncio.gather(cancel_task, return_exceptions=True)
                        raise cancelled from None
                    except RuntimeError:
                        # Forced close has already fenced the generation.
                        # Teardown is local-only and must not emit a Scotty
                        # cancel against reopened resources.
                        raise cancelled from None
                    raise
                while True:
                    try:
                        outcome = await asyncio.shield(finalize_task)
                    except asyncio.CancelledError:
                        # A second caller cancellation must not abandon the
                        # already-dispatched finalize or its body descriptor.
                        # Forced close may instead cancel the child itself;
                        # its completed cancellation already proves settlement.
                        if finalize_task.done():
                            break
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "Background finalize POST failed before cancellation propagated: %r",
                            exc,
                        )
                        break
                    else:
                        if outcome.error is not None:
                            logger.debug(
                                "Background finalize POST failed before cancellation propagated: %r",
                                outcome.error,
                            )
                        break
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
        auth_route: str,
        *,
        logger: Any,
        _expected_epoch: int,
    ) -> None:
        """Best-effort POST a Scotty resumable-upload cancel command.

        The headers are built *below* the validation call on purpose:
        ``Origin``/``Referer`` are derived from the validated upload URL, so an
        untrusted server-named host must never reach an outbound header.
        """
        try:
            self.assert_epoch(_expected_epoch)
            upload_url = _validate_resumable_upload_url(upload_url)
            origin = _upload_url_origin(upload_url)
            headers = {
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                "x-goog-authuser": auth_route,
                "Origin": origin,
                "Referer": f"{origin}/",
                "x-goog-upload-command": "cancel",
            }
            cookies = self._live_cookies(_expected_epoch)
            self.assert_epoch(_expected_epoch)
            client = self._client_factory()(
                timeout=httpx.Timeout(10.0, read=10.0),
                cookies=cookies,
            )
            self._track_transport_client(client, _expected_epoch)
            try:
                async with client:
                    self.assert_epoch(_expected_epoch)
                    await client.post(upload_url, headers=headers)
            finally:
                self._transport_clients.discard(client)
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
