"""Private source file upload pipeline."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import IO, TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import SplitResult, parse_qsl, urlsplit

import httpx

from ._callbacks import maybe_await_callback
from ._env import get_base_url
from ._idempotency import idempotent_create
from ._runtime_config import (
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    normalize_max_concurrent_uploads,
)
from ._runtime_contracts import (
    AuthMetadata,
    Kernel,
    RpcCaller,
)
from ._source_listing import SourceLister
from ._source_polling import SourcePoller
from ._source_upload_payloads import (
    build_register_file_source_params,
    build_rename_source_params,
    build_resumable_upload_start_request,
)
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
    from ._runtime_lifecycle import ClientLifecycle
    from ._transport_drain import TransportDrainTracker

_SOURCE_ID_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SOURCE_ID_FIELD_NAMES = frozenset({"SOURCE_ID", "source_id", "sourceId"})
_CONTEXTUAL_SOURCE_ID_FIELD_NAMES = frozenset({"id"})
_SOURCE_NAME_FIELD_NAMES = frozenset(
    {"SOURCE_NAME", "source_name", "sourceName", "filename", "fileName", "name", "title"}
)
_SOURCE_ID_ENVELOPE_MAX_DEPTH = 8


class RpcCallback(Protocol):
    """RPC callback shape used by upload registration.

    Structurally distinct from :class:`notebooklm._runtime_contracts.RpcCaller`:
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


_INVALID_ARGUMENT_RPC_CODE = 3
_SOURCE_LIMIT_HINT_FLOOR = 50
_TIER_SOURCE_LIMITS_SUMMARY = "50/100/300/600"
# Preserve the historical ``notebooklm._sources`` log channel after moving
# upload choreography into this module.
module_logger = logging.getLogger("notebooklm").getChild("_sources")


def _normalize_upload_path(path: str) -> str:
    return (path or "/").rstrip("/") + "/"


def _default_port_for_scheme(scheme: str) -> int | None:
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


def _redacted_upload_authority(parsed: SplitResult) -> str | None:
    host = parsed.hostname
    if host is None:
        return None

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    port = parsed.port
    port_suffix = f":{port}" if port is not None else ""
    return f"{host}{port_suffix}"


def _redact_upload_url(upload_url: str) -> str:
    """Return a log-safe representation of a resumable upload URL."""
    try:
        parsed = urlsplit(upload_url)
        authority = _redacted_upload_authority(parsed)
    except ValueError:
        return "[REDACTED_UPLOAD_URL]"
    if not parsed.scheme or authority is None:
        return "[REDACTED_UPLOAD_URL]"
    suffix = "?..." if parsed.query else ""
    return f"{parsed.scheme}://{authority}{parsed.path}{suffix}"


def _validate_resumable_upload_url(upload_url: str) -> str:
    """Validate that a resumable upload URL targets the configured upload endpoint."""
    try:
        parsed = urlsplit(upload_url)
        actual_port = parsed.port or _default_port_for_scheme(parsed.scheme)
        expected = urlsplit(get_upload_url())
        expected_port = expected.port or _default_port_for_scheme(expected.scheme)
    except ValueError as exc:
        raise ValidationError("Upload URL is not valid") from exc

    if parsed.scheme != "https":
        raise ValidationError("Upload URL must use https")
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError("Upload URL must not contain credentials")
    if parsed.hostname is None:
        raise ValidationError("Upload URL must include a host")
    if parsed.hostname != expected.hostname or actual_port != expected_port:
        raise ValidationError("Upload URL host is not trusted")
    if _normalize_upload_path(parsed.path) != _normalize_upload_path(expected.path):
        raise ValidationError("Upload URL path is not trusted")
    upload_ids = [
        value
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() == "upload_id"
    ]
    if len(upload_ids) != 1 or not upload_ids[0]:
        raise ValidationError("Upload URL must include exactly one non-empty upload_id")

    return upload_url


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
_HTML_UPLOAD_SUFFIXES = frozenset({".html", ".htm", ".xhtml", ".xht"})
_HTML_UPLOAD_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})


# Audit CC6: single-loop-per-client invariant per ADR-004; not safe for multi-loop fan-out.
_BACKGROUND_CANCEL_TASKS: set[asyncio.Task[None]] = set()


def _retain_background_cancel_task(task: asyncio.Task[None]) -> None:
    _BACKGROUND_CANCEL_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_CANCEL_TASKS.discard)


def _extract_register_file_source_id(result: Any, filename: str) -> str | None:
    """Locate the SOURCE_ID string in an ADD_SOURCE_FILE response.

    Only trusted ADD_SOURCE_FILE shapes are accepted: explicit source-id fields
    and the legacy singleton list envelope (``[[id]]`` / ``[[[[id]]]]``).
    Arbitrary nested ids are intentionally ignored so ambiguous responses fall
    through to the post-register source-list probe.
    """
    field_candidates = _extract_source_id_field_candidates(result, filename)
    if len(field_candidates) == 1:
        return field_candidates[0]
    if len(field_candidates) > 1:
        return None

    row_candidates = _extract_contextual_source_id_row_candidates(result, filename)
    if len(row_candidates) == 1:
        return row_candidates[0]
    if len(row_candidates) > 1:
        return None

    prefixed_candidate = _extract_prefixed_singleton_source_id_envelope(result, filename)
    if prefixed_candidate is not None:
        return prefixed_candidate

    return _extract_singleton_source_id_envelope(result, filename)


def _extract_source_id_field_candidates(result: Any, filename: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(value: Any) -> None:
        candidate = _coerce_source_id_candidate(value, filename)
        if candidate is not None and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)

    def walk(node: Any, depth: int) -> None:
        if depth > _SOURCE_ID_ENVELOPE_MAX_DEPTH:
            return
        if isinstance(node, dict):
            names = _source_context_names(node)
            matched_context = bool(names) and any(
                _coerce_filename_candidate(name) == filename for name in names
            )
            mismatched_context = bool(names) and not matched_context
            for key, value in node.items():
                if not isinstance(key, str):
                    continue
                if (
                    key in _SOURCE_ID_FIELD_NAMES
                    and not mismatched_context
                    and (depth == 0 or matched_context)
                ) or (key in _CONTEXTUAL_SOURCE_ID_FIELD_NAMES and matched_context):
                    add_candidate(value)
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1)

    walk(result, 0)
    return candidates


def _extract_singleton_source_id_envelope(result: Any, filename: str) -> str | None:
    node, depth = _unwrap_singleton_envelope(result)
    if depth == 0:
        return None

    return _coerce_source_id_candidate(node, filename)


def _extract_prefixed_singleton_source_id_envelope(result: Any, filename: str) -> str | None:
    if not isinstance(result, list) or len(result) != 2 or result[0] is not None:
        return None

    return _extract_singleton_source_id_envelope(result[1], filename)


def _extract_contextual_source_id_row_candidates(result: Any, filename: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(value: Any) -> None:
        candidate = _coerce_source_id_candidate(value, filename)
        if candidate is not None and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)

    def walk(node: Any, depth: int) -> None:
        if depth > _SOURCE_ID_ENVELOPE_MAX_DEPTH:
            return
        if isinstance(node, list):
            if len(node) >= 2:
                if _coerce_filename_candidate(node[1]) == filename:
                    add_candidate(node[0])
                if _coerce_filename_candidate(node[0]) == filename:
                    add_candidate(node[1])
            for child in node:
                walk(child, depth + 1)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)

    walk(result, 0)
    return candidates


def _coerce_filename_candidate(value: Any) -> str | None:
    value, _depth = _unwrap_singleton_envelope(value)
    if not isinstance(value, str):
        return None
    return value.strip()


def _coerce_source_id_candidate(value: Any, filename: str) -> str | None:
    value, _depth = _unwrap_singleton_envelope(value)
    if not isinstance(value, str):
        return None
    if len(value) > 1000:
        return None
    candidate = value.strip()
    if not candidate or candidate == filename:
        return None
    if _SOURCE_ID_UUID_PATTERN.match(candidate) or _looks_like_id_string(candidate):
        return candidate
    return None


def _source_context_names(node: dict[Any, Any]) -> list[Any]:
    return [
        value
        for key, value in node.items()
        if isinstance(key, str) and key in _SOURCE_NAME_FIELD_NAMES
    ]


def _unwrap_singleton_envelope(value: Any) -> tuple[Any, int]:
    depth = 0
    while isinstance(value, list) and len(value) == 1 and depth < _SOURCE_ID_ENVELOPE_MAX_DEPTH:
        value = value[0]
        depth += 1
    return value, depth


def _register_response_shape_label(result: Any) -> str:
    if isinstance(result, dict):
        return "object"
    if isinstance(result, list):
        return "array"
    if isinstance(result, str):
        return "string"
    if result is None:
        return "null"
    return type(result).__name__


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


def _normalize_content_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _transient_error_types_for_upload(content_type: str) -> tuple[int | None, ...]:
    """Return source status=ERROR transient policy for this upload."""
    normalized = _normalize_content_type(content_type)
    if (
        normalized.startswith(_MEDIA_CONTENT_TYPE_PREFIXES)
        or normalized in _MEDIA_APPLICATION_CONTENT_TYPES
    ):
        return _MEDIA_TRANSIENT_ERROR_TYPES
    return _STRICT_TRANSIENT_ERROR_TYPES


def _validate_upload_file_supported(file_path: Path, content_type: str) -> None:
    """Reject local file types known to fail NotebookLM's upload endpoint."""
    normalized = _normalize_content_type(content_type)
    if (
        file_path.suffix.lower() in _HTML_UPLOAD_SUFFIXES
        or normalized in _HTML_UPLOAD_CONTENT_TYPES
    ):
        raise ValidationError(
            "HTML file uploads are not supported by NotebookLM's upload endpoint: "
            f"{file_path.name}. Convert the page to .txt, .md, or .pdf first, then retry."
        )


class SourceUploadPipeline:
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
        self._bound_loop: asyncio.AbstractEventLoop | None = None
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

        Called from ``SourcesAPI.__init__``
        (alongside :meth:`configure_source_limit_lookup`) so the pipeline's
        source-lifecycle verbs (``list_sources`` / ``get_source`` /
        ``wait_until_ready`` / ``wait_until_registered``) delegate to the
        SAME ``SourceLister`` / ``SourcePoller`` instances the public
        ``SourcesAPI`` uses, instead of parallel copies built in the
        pipeline constructor. Direct callers that never run through
        ``SourcesAPI`` keep the freshly-constructed defaults.
        """
        self._lister = lister
        self._poller = poller

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

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Capture or clear the event-loop binding for the upload semaphore.

        Called by :meth:`ClientLifecycle.open` after it captures the running
        loop, mirroring the identically-named method on
        :class:`notebooklm._client_composed.ClientComposed`,
        :class:`TransportDrainTracker`, :class:`ReqidCounter`, and
        :class:`AuthRefreshCoordinator`. Passing ``None`` clears the binding
        for the next ``open()`` (which rebinds to a fresh loop).

        When the loop actually changes, the cached semaphore is discarded here
        too so this method is self-consistent even if called independently of
        :meth:`reset_after_open` (e.g. directly in a test or a future caller):
        a stale semaphore bound to the old loop must never be reused after a
        rebind. The production ``open()`` path also calls
        :meth:`reset_after_open` immediately after, so the discard is
        idempotent there.

        The cross-loop guard for ``add_file`` is the lifecycle's
        ``assert_bound_loop`` (already called at the top of :meth:`add_file`);
        this binding only governs when the lazy semaphore is rebuilt.
        """
        if loop is not self._bound_loop:
            self._upload_semaphore = None
        self._bound_loop = loop

    def reset_after_open(self) -> None:
        """Discard the lazy upload semaphore so a reopened client rebinds it.

        Called from :meth:`ClientLifecycle.open` (alongside the
        per-collaborator ``set_bound_loop`` propagation) so a client that was
        closed and reopened on a *different* event loop builds a fresh
        ``asyncio.Semaphore`` on the new loop instead of reusing the stale one
        bound to the old (now-dead) loop. On Python 3.10/3.11 reusing the
        stale semaphore can raise "bound to a different event loop" or mispark
        waiters; on 3.12+ the breakage is largely masked, but resetting keeps
        the behaviour consistent across versions.

        Mirrors :meth:`notebooklm._client_composed.ClientComposed.reset_after_open`.
        Deliberately narrow: dropping the reference is enough because the
        semaphore is reconstructed lazily on the next
        :meth:`get_upload_semaphore` call from inside the new loop.
        ``max_concurrent_uploads`` is left untouched.
        """
        self._upload_semaphore = None

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
        """Add a file source to a notebook using resumable upload.

        Raises ``ValidationError`` for HTML-family uploads because
        NotebookLM's upload endpoint rejects those file extensions.
        """
        # Audit C1: catch cross-loop add_file *before* touching
        # ``operation_scope`` or lazily allocating the upload semaphore.
        # Both are loop-bound on first use, so a cross-loop call would
        # otherwise attach a primitive to the wrong loop before the
        # documented ``RuntimeError`` guard fires (ADR-004).
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
            response.raise_for_status()

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
