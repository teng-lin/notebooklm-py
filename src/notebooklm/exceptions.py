"""Exceptions for notebooklm-py.

All library exceptions inherit from NotebookLMError, allowing users to catch
all library errors with a single except clause.

Stability: NotebookLMError and its direct subclasses are part of the public API.

Example:
    try:
        await client.notebooks.list()
    except NotebookLMError as e:
        handle_error(e)
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from ._env import DEFAULT_BASE_URL, get_base_url
from ._logging import scrub_secrets

if TYPE_CHECKING:
    from ._types.artifacts import GenerationStatus

ArtifactStalledPhase = Literal["pending", "in_progress"]


_PREVIEW_LIMIT = 80
# Pre-slice cap for the truncated path: scrub at most this many chars before
# cutting to ``_PREVIEW_LIMIT``. The 10x window gives a boundary-straddling
# secret ample room to be neutralized before the 80-char cut (mirrors the
# two-stage slice in ``AuthExtractionError`` below), while bounding the regex
# sweep at O(800 chars) instead of O(len(raw)) for multi-MB error bodies.
_PREVIEW_SCRUB_CAP = _PREVIEW_LIMIT * 10


def _truncate_response_preview(raw: str | None) -> str | None:
    """Truncate a raw RPC response preview for safe display in error contexts.

    Default behavior keeps the preview compact (80 chars + ``"..."`` suffix) so
    error logs and CLI output stay readable. Set ``NOTEBOOKLM_DEBUG=1`` to opt
    into the full untruncated body for deep debugging.

    Credential-shaped substrings (CSRF tokens, session cookies, etc.) are
    scrubbed *before* truncation in both modes. ``raw_response`` is a public
    attribute spliced into ``str()``/``repr()`` of RPC errors, so it escapes the
    logging pipeline's ``RedactingFilter`` and must be sanitized at the source.

    In the default (truncated) path the input is pre-sliced to
    ``_PREVIEW_SCRUB_CAP`` before scrubbing so a multi-MB error body does not
    pay for a full regex sweep just to discard all but the first 80 chars. The
    ``NOTEBOOKLM_DEBUG=1`` path keeps the whole body, so it scrubs the full
    string.
    """
    if raw is None:
        return None
    if os.environ.get("NOTEBOOKLM_DEBUG") == "1":
        return scrub_secrets(raw)
    scrubbed = scrub_secrets(raw[:_PREVIEW_SCRUB_CAP])
    if len(scrubbed) > _PREVIEW_LIMIT:
        return scrubbed[:_PREVIEW_LIMIT] + "..."
    return scrubbed


__all__ = [
    # Base
    "NotebookLMError",
    # Cross-domain umbrellas
    "NotFoundError",
    "WaitTimeoutError",
    # Validation/Config
    "ValidationError",
    "ConfigurationError",
    # Network (NOT under RPC - happens before RPC)
    "NetworkError",
    # RPC Protocol
    "RPCError",
    "DecodingError",
    "UnknownRPCMethodError",
    "AuthError",
    "AuthExtractionError",
    "RateLimitError",
    "ServerError",
    "ClientError",
    "RPCTimeoutError",
    "RPCResponseTooLargeError",
    # Idempotency
    "NonIdempotentRetryError",
    "IdempotencyVariantError",
    # Domain: Notebooks
    "NotebookError",
    "NotebookNotFoundError",
    "NotebookLimitError",
    # Domain: Chat
    "ChatError",
    "ChatResponseParseError",
    # Domain: Sources
    "SourceError",
    "SourceAddError",
    "SourceNotFoundError",
    "SourceProcessingError",
    "SourceTimeoutError",
    # Domain: Artifacts
    "ArtifactError",
    "ArtifactNotFoundError",
    "ArtifactNotReadyError",
    "ArtifactParseError",
    "ArtifactDownloadError",
    "ArtifactFeatureUnavailableError",
    "ArtifactTimeoutError",
    "ArtifactPendingTimeoutError",
    "ArtifactInProgressTimeoutError",
    # Domain: Research
    "ResearchError",
    "ResearchTimeoutError",
    "ResearchTaskMismatchError",
    # Domain: Notes
    "NoteError",
    "NoteNotFoundError",
    # Domain: Mind maps
    "MindMapError",
    "MindMapNotFoundError",
]


# =============================================================================
# Base Exception
# =============================================================================


class NotebookLMError(Exception):
    """Base exception for all notebooklm-py errors.

    Users can catch all library errors with:
        try:
            await client.notebooks.list()
        except NotebookLMError as e:
            handle_error(e)
    """


# =============================================================================
# Cross-domain umbrellas
# =============================================================================


class NotFoundError(NotebookLMError):
    """Common base for resource-not-found exceptions.

    Catch this to handle any not-found case across notebooks, sources,
    and artifacts in one ``except`` clause::

        try:
            notebook = await client.notebooks.get(nb_id)
            source = await client.sources.wait_until_ready(nb_id, src_id)
            await client.artifacts.download_audio(nb_id, dest, audio_id)
        except NotFoundError as e:
            # Catches NotebookNotFoundError, SourceNotFoundError,
            # ArtifactNotFoundError, and MindMapNotFoundError uniformly (and
            # NoteNotFoundError once its not-found paths land in v0.8.0).
            handle_missing_resource(e)

    The example uses methods that *raise* a ``*NotFoundError`` on missing
    IDs (:meth:`NotebooksAPI.get`, :meth:`SourcesAPI.wait_until_ready`,
    the artifact download / content paths). :meth:`SourcesAPI.get` and
    :meth:`ArtifactsAPI.get` instead return ``None`` for missing IDs — use
    them when you want a lookup that does not trigger the umbrella.
    :class:`MindMapNotFoundError` is raised by the ``client.mind_maps``
    mutation paths (issue #1291); :class:`NoteNotFoundError` is defined but
    not raised by any method yet (the prerequisite for the v0.8.0 work).

    Subclasses retain their existing type-specific bases — for example,
    :class:`SourceNotFoundError` is still a :class:`SourceError`, and
    :class:`NotebookNotFoundError` is still an :class:`RPCError` and a
    :class:`NotebookError`. This umbrella is additive and does not
    change existing catch semantics.

    .. note::

        As of v0.6.0, every concrete ``*NotFoundError`` subclass also mixes in
        :class:`RPCError`, so ``except RPCError`` catches each of them
        uniformly. See the v0.6.0 BREAKING-CHANGE entry in CHANGELOG.md
        for migration guidance (the broad ``except RPCError`` clause now
        intercepts a missing source / artifact that previously fell
        through to the specific ``*NotFoundError`` handler).
    """


class WaitTimeoutError(NotebookLMError, TimeoutError):
    """Common base for *wait/poll* timeouts across the library.

    Every ``wait_*`` / polling method that gives up after a time budget raises
    a subclass of this umbrella, so callers can catch any wait timeout — source
    readiness, artifact generation, or research completion — in one clause::

        try:
            await client.sources.wait_until_ready(nb_id, src_id)
            await client.artifacts.wait_for_completion(nb_id, task_id)
            await client.research.wait_for_completion(nb_id, task_id)
        except WaitTimeoutError as e:
            # Catches SourceTimeoutError, ArtifactTimeoutError (and its
            # pending/in-progress subclasses), and ResearchTimeoutError.
            handle_wait_timeout(e)

    ``WaitTimeoutError`` also mixes in the built-in :class:`TimeoutError`, so
    existing ``except TimeoutError`` clauses keep catching every wait timeout
    exactly as before — this umbrella is *additive*. It widens the inheritance
    of :class:`SourceTimeoutError`, :class:`ArtifactTimeoutError` (and its
    :class:`ArtifactPendingTimeoutError` / :class:`ArtifactInProgressTimeoutError`
    subclasses), and :class:`ResearchTimeoutError`; their existing domain bases
    (``SourceError`` / ``ArtifactError`` / ``ResearchError``) are unchanged, so
    every prior ``except`` clause continues to work.

    .. note::

        Added in v0.7.0. ``ArtifactsAPI.wait_for_completion`` and
        ``ResearchAPI.wait_for_completion`` previously raised
        :class:`ArtifactTimeoutError` (already a ``TimeoutError``) and the bare
        built-in :class:`TimeoutError` respectively. Routing the research path
        through :class:`ResearchTimeoutError` (a ``WaitTimeoutError`` and thus
        still a ``TimeoutError``) is backward-compatible for ``except
        TimeoutError`` callers and newly catchable via the umbrella.
    """


# =============================================================================
# Validation/Configuration
# =============================================================================


class ValidationError(NotebookLMError):
    """Invalid user input or parameters."""


class ConfigurationError(NotebookLMError):
    """Missing or invalid configuration (auth, storage)."""


# =============================================================================
# Network (NOT under RPC - happens before RPC processing)
# =============================================================================


class NetworkError(NotebookLMError):
    """Connection failures, DNS errors, timeouts before RPC.

    Users may want to retry on NetworkError but not on RPCError.

    Attributes:
        method_id: The RPC method ID that failed (if known).
        original_error: The underlying network exception.
    """

    def __init__(
        self,
        message: str,
        *,
        method_id: str | None = None,
        original_error: Exception | None = None,
    ):
        super().__init__(message)
        self.method_id = method_id
        self.original_error = original_error


# =============================================================================
# RPC Protocol
# =============================================================================


class RPCError(NotebookLMError):
    """Base for RPC-specific failures after connection established.

    Note:
        Domain-level "not found" exceptions — :class:`NotebookNotFoundError`,
        :class:`SourceNotFoundError`, :class:`ArtifactNotFoundError` — inherit
        from :class:`RPCError` so that ``except RPCError`` keeps catching them
        at transport-level call sites. The underlying RPC call succeeded but
        returned a degenerate / empty payload identifying the resource as
        missing. When writing ``except RPCError`` clauses, be aware these
        domain errors may also flow through; catch the specific domain type
        BEFORE the broad ``except RPCError`` clause if you want to handle them
        differently.

        **v0.6.0 BREAKING CHANGE:** before v0.6.0, only
        :class:`NotebookNotFoundError` mixed in :class:`RPCError`;
        :class:`SourceNotFoundError` and :class:`ArtifactNotFoundError` did
        not. v0.6.0 restores symmetry by adding :class:`RPCError` to both.
        Code that catches ``RPCError`` *before* the specific
        ``except SourceNotFoundError`` / ``except ArtifactNotFoundError``
        clauses will now intercept what previously fell through. Reorder your
        ``except`` clauses to put the more specific exceptions first.

    Attributes:
        method_id: The RPC method ID (e.g., "abc123") for debugging.
        raw_response: First 80 chars of raw response for debugging
            (with ``"..."`` suffix if truncated). Credential-shaped substrings
            are scrubbed before truncation, so this attribute is safe to splice
            into ``str()``/``repr()``. Set ``NOTEBOOKLM_DEBUG=1`` to preserve
            the full body (still scrubbed).
        rpc_code: Google's internal error code if available.
        found_ids: List of RPC IDs found in the response (for debugging).
    """

    def __init__(
        self,
        message: str,
        *,
        method_id: str | None = None,
        raw_response: str | None = None,
        rpc_code: str | int | None = None,
        found_ids: list[str] | None = None,
    ):
        super().__init__(message)
        self.method_id = method_id
        self.raw_response = _truncate_response_preview(raw_response)
        self.rpc_code = rpc_code
        self.found_ids = found_ids or []

    # Backward compatibility aliases
    @property
    def rpc_id(self) -> str | None:
        """Permanent backward-compatibility alias for ``method_id``.

        Exception diagnostic aliases are exempt from the standard deprecation
        cycle because removal can mask the original exception inside
        ``except`` handlers.
        """
        return self.method_id

    @property
    def code(self) -> str | int | None:
        """Permanent backward-compatibility alias for ``rpc_code``.

        Exception diagnostic aliases are exempt from the standard deprecation
        cycle because removal can mask the original exception inside
        ``except`` handlers.
        """
        return self.rpc_code


class DecodingError(RPCError):
    """Failed to parse RPC response structure.

    This indicates the API returned data in an unexpected format.
    """


class UnknownRPCMethodError(DecodingError):
    """RPC response structure doesn't match expectations.

    This often indicates Google has changed the API. Check for library updates.

    Carries structured context to help diagnose schema drift:

    Attributes:
        method_id: The RPC method ID that was requested (or that drifted).
        path: Index path inside the decoded payload at which descent failed
            (empty tuple for top-level drift, ``(0, 2)`` for nested, etc.).
        source: Caller-provided label identifying which decoder/helper raised
            this error (e.g. ``"_notebooks.list"``).
        found_ids: When raised by the response-level decoder, the list of RPC
            IDs actually present in the response.
        raw_response: First 80 chars of the raw response, when available
            (``NOTEBOOKLM_DEBUG=1`` preserves the full body). The string branch
            is secret-scrubbed before truncation; non-string payloads are stored
            as-is on this subclass.
        data_at_failure: Truncated repr (~200 chars) of the data the helper
            was attempting to index into when descent failed.
    """

    def __init__(
        self,
        message: str = "",
        *,
        method_id: str | int | None = None,
        path: tuple[int, ...] | None = None,
        source: str | None = None,
        found_ids: list[int | str] | None = None,
        raw_response: Any | None = None,
        data_at_failure: Any | None = None,
        rpc_code: str | int | None = None,
    ):
        # Coerce method_id to str for the base RPCError contract while
        # preserving the original (possibly int) value on this subclass.
        base_method_id = str(method_id) if method_id is not None else None
        # Normalize found_ids to list[str] for the base contract while
        # keeping the typed list[int | str] on this subclass.
        base_found_ids: list[str] | None = (
            None if found_ids is None else [str(item) for item in found_ids]
        )
        # raw_response on RPCError is str | None; only forward when stringy.
        base_raw_response = raw_response if isinstance(raw_response, str) else None
        super().__init__(
            message,
            method_id=base_method_id,
            raw_response=base_raw_response,
            rpc_code=rpc_code,
            found_ids=base_found_ids,
        )
        # Preserve original typed values on this subclass.
        self.method_id = method_id  # type: ignore[assignment]
        self.path = path
        self.source = source
        # Override base found_ids with the typed list (may contain ints).
        if found_ids is not None:
            self.found_ids = found_ids  # type: ignore[assignment]
        # The base class already truncated the string branch via
        # ``_truncate_response_preview`` (see ``base_raw_response`` above).
        # Only override here for non-string payloads (dict/list/etc) supported
        # by this subclass's widened ``Any`` type — those bypass the base
        # class's ``str | None`` contract entirely.
        if not isinstance(raw_response, str):
            self.raw_response = raw_response
        self.data_at_failure = data_at_failure

    def __str__(self) -> str:
        base = super().__str__()
        extras: list[str] = []
        if self.method_id is not None:
            extras.append(f"method_id={self.method_id!r}")
        if self.path is not None:
            extras.append(f"path={self.path!r}")
        if self.source is not None:
            extras.append(f"source={self.source!r}")
        if self.found_ids:
            extras.append(f"found_ids={self.found_ids!r}")
        if self.data_at_failure is not None:
            extras.append(f"data_at_failure={self.data_at_failure!r}")
        if not extras:
            return base
        return f"{base} [{', '.join(extras)}]" if base else ", ".join(extras)

    def __repr__(self) -> str:
        return (
            f"UnknownRPCMethodError("
            f"message={super().__str__()!r}, "
            f"method_id={self.method_id!r}, "
            f"path={self.path!r}, "
            f"source={self.source!r}, "
            f"found_ids={self.found_ids!r}, "
            f"data_at_failure={self.data_at_failure!r})"
        )


class AuthError(RPCError):
    """Authentication or authorization failure.

    Attributes:
        recoverable: True if re-authentication might help (e.g., token expired).
    """

    recoverable: bool = False


class AuthExtractionError(RPCError):
    """Failed to extract a required field from the NotebookLM HTML response.

    Raised when token extraction (e.g., ``SNlM0e``, ``FdrFJe``) cannot locate
    the expected ``WIZ_global_data`` key. Most commonly indicates that Google
    has changed the embedded JavaScript structure on the homepage — i.e.
    schema drift — and the regex patterns must be updated.

    Carries a sanitized preview of the HTML response so operators can diagnose
    drift without re-running the CLI to capture the page.

    Attributes:
        key: The ``WIZ_global_data`` field name that could not be extracted
            (e.g., ``"SNlM0e"`` or ``"FdrFJe"``).
        payload_preview: First 200 characters of the response HTML used to
            attempt extraction. Whitespace is collapsed for readability.
    """

    PREVIEW_LENGTH = 200

    def __init__(
        self,
        key: str,
        payload_preview: str,
        *,
        message: str | None = None,
    ):
        self.key = key
        # Two-stage slice with the scrub in the middle, so we bound regex work
        # without giving up boundary-straddle safety:
        #
        # 1. Pre-slice to a generous 10x cap. Bounds the scrub at O(2000 chars)
        #    instead of O(len(payload)) — a multi-MB HTML body would otherwise
        #    cost ~7 regex passes over the whole thing just to throw most away.
        # 2. Scrub the slice. A secret straddling the 10x boundary is
        #    theoretically possible but the 2000-char window gives ~19x more
        #    slack than the 5x preview limit, so any realistic ``f.sid=``,
        #    ``Bearer ...``, or ``Set-Cookie:`` value fits well inside.
        # 3. Re-slice to 5x. The scrub already neutralized anything that would
        #    have leaked from the 5x cut, including secrets that originally
        #    straddled the 5x boundary inside the 10x window.
        pre_sliced = payload_preview[: self.PREVIEW_LENGTH * 10]
        scrubbed = scrub_secrets(pre_sliced)
        head = scrubbed[: self.PREVIEW_LENGTH * 5]
        # Collapse runs of whitespace so the preview stays compact and useful
        # even when the upstream HTML is heavily indented or contains newlines.
        collapsed = re.sub(r"\s+", " ", head).strip()
        self.payload_preview = collapsed[: self.PREVIEW_LENGTH]
        # Default message is human-readable and includes both the failing key
        # and the sanitized preview — this is the diagnostic that operators
        # see in logs and exception traces.
        rendered = message or (
            f"Failed to extract {key!r} from NotebookLM HTML response. "
            f"This usually means Google changed the page structure. "
            f"Preview: {self.payload_preview!r}"
        )
        super().__init__(rendered)


class RateLimitError(RPCError):
    """Rate limit exceeded.

    Attributes:
        retry_after: Seconds to wait before retrying (if provided by API).
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        method_id: str | None = None,
        raw_response: str | None = None,
        rpc_code: str | int | None = None,
        found_ids: list[str] | None = None,
    ):
        super().__init__(
            message,
            method_id=method_id,
            raw_response=raw_response,
            rpc_code=rpc_code,
            found_ids=found_ids,
        )
        self.retry_after = retry_after


class ServerError(RPCError):
    """Server-side error (5xx responses).

    Attributes:
        status_code: HTTP status code (500-599).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method_id: str | None = None,
        raw_response: str | None = None,
        rpc_code: str | int | None = None,
        found_ids: list[str] | None = None,
    ):
        super().__init__(
            message,
            method_id=method_id,
            raw_response=raw_response,
            rpc_code=rpc_code,
            found_ids=found_ids,
        )
        self.status_code = status_code


class ClientError(RPCError):
    """Client-side error (4xx responses, excluding auth/rate limit).

    Attributes:
        status_code: HTTP status code (400-499).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method_id: str | None = None,
        raw_response: str | None = None,
        rpc_code: str | int | None = None,
        found_ids: list[str] | None = None,
    ):
        super().__init__(
            message,
            method_id=method_id,
            raw_response=raw_response,
            rpc_code=rpc_code,
            found_ids=found_ids,
        )
        self.status_code = status_code


class RPCTimeoutError(NetworkError):
    """RPC request timed out.

    Inherits from NetworkError since timeout is a transport-level issue.

    Attributes:
        timeout_seconds: The timeout duration that was exceeded.
    """

    def __init__(
        self,
        message: str,
        *,
        timeout_seconds: float | None = None,
        method_id: str | None = None,
        original_error: Exception | None = None,
    ):
        super().__init__(
            message,
            method_id=method_id,
            original_error=original_error,
        )
        self.timeout_seconds = timeout_seconds


class RPCResponseTooLargeError(RPCError):
    """RPC response body exceeded the configured maximum size.

    Raised by the streaming transport when a response body grows past
    ``MAX_RPC_RESPONSE_BYTES`` (currently 50 MiB) while being read. The guard
    aborts the read mid-stream rather than buffering an unbounded body, so a
    runaway or hostile server can't exhaust process memory.

    Attributes:
        limit_bytes: The configured maximum (in bytes) that was exceeded.
        bytes_read: Number of bytes already buffered when the guard fired
            (always strictly greater than ``limit_bytes``).
    """

    def __init__(
        self,
        message: str,
        *,
        limit_bytes: int | None = None,
        bytes_read: int | None = None,
        method_id: str | None = None,
    ):
        super().__init__(message, method_id=method_id)
        self.limit_bytes = limit_bytes
        self.bytes_read = bytes_read


# =============================================================================
# Idempotency
# =============================================================================


class NonIdempotentRetryError(NotebookLMError):
    """Raised when an opt-in idempotent call cannot guarantee single-write semantics.

    Some create RPCs (notably ``SourcesAPI.add_text``) lack a reliable
    server-side dedupe key, so a probe-then-retry strategy cannot
    guarantee single-write semantics under transport failures. Callers
    that opt in via ``idempotent=True`` get this error rather than a
    silent duplicate-resource on retry.

    See ``docs/python-api.md#idempotency`` for guidance on building
    idempotent text-source workflows.
    """


class IdempotencyVariantError(NotebookLMError):
    """Raised when an unknown ``operation_variant`` is requested for an RPC
    that has explicit variant-table entries.

    The mutating-RPC idempotency registry keys policies on
    ``(RPCMethod, operation_variant | None)``. When a method has variant
    entries (e.g. ``"upsert"``, ``"overwrite"``) AND the caller supplies a
    variant name that is not in that table, the registry MUST raise this
    error rather than silently falling back to the ``(method, None)``
    default — silent fallback would hide caller typos and API drift.

    Methods that only have a ``(method, None)`` entry tolerate any variant
    name (the variant table is effectively empty, so there is no typo to
    catch). See :func:`notebooklm._idempotency.IdempotencyRegistry.get_entry`.
    """


# =============================================================================
# Domain: Notebooks
# =============================================================================


class NotebookError(NotebookLMError):
    """Base for notebook operations."""


class NotebookNotFoundError(NotFoundError, RPCError, NotebookError):
    """Notebook not found.

    Inherits from :class:`NotFoundError`, :class:`RPCError`, and
    :class:`NotebookError` so callers can catch any of them. The
    :class:`NotFoundError` umbrella catches this alongside
    :class:`SourceNotFoundError` and :class:`ArtifactNotFoundError`. The RPC
    base is what ``client.notebooks.get`` raises when the server returns an
    empty / degenerate payload for a missing ID, so ``except RPCError`` keeps
    working at call sites that handle transport-level failures.
    ``except NotebookError`` continues to work at domain-level call sites that
    don't care about the RPC layer.

    Attributes:
        notebook_id: The ID that was not found.
        method_id: The RPC method ID (inherited from :class:`RPCError`).
        raw_response: First 80 chars of the raw response, if any
            (``NOTEBOOKLM_DEBUG=1`` preserves the full body).
    """

    def __init__(
        self,
        notebook_id: str,
        *,
        method_id: str | None = None,
        raw_response: str | None = None,
    ):
        self.notebook_id = notebook_id
        super().__init__(
            f"Notebook not found: {notebook_id}",
            method_id=method_id,
            raw_response=raw_response,
        )


class NotebookLimitError(NotebookError):
    """Notebook quota appears to be exhausted.

    Attributes:
        current_count: Number of owned notebooks returned by the list API.
        limit: Server-reported NotebookLM notebook limit, if known.
        known_limits: Optional known NotebookLM notebook limits to include in output.
        original_error: The underlying RPC failure from create.
    """

    def __init__(
        self,
        current_count: int,
        *,
        limit: int | None = None,
        known_limits: tuple[int, ...] = (),
        original_error: RPCError | None = None,
    ):
        self.current_count = current_count
        self.limit = limit
        self.known_limits = known_limits
        self.original_error = original_error

        count_text = str(current_count)
        if limit is not None:
            count_text = f"{current_count}/{limit}"

        known_text = ", ".join(str(value) for value in known_limits)
        try:
            base_url = get_base_url()
        except ValueError:
            base_url = DEFAULT_BASE_URL
        message = (
            "Cannot create notebook: account appears to be at or very near the "
            f"NotebookLM notebook limit ({count_text} owned notebooks reported). "
            f"Delete old notebooks at {base_url} and try again."
        )
        if known_limits:
            message += f" Known NotebookLM limits include: {known_text}."
        if original_error is not None:
            message += f" Original RPC error: {original_error}"
        super().__init__(message)

    def to_error_response_extra(self) -> dict[str, Any]:
        """Return structured fields for CLI JSON error responses."""
        extra: dict[str, Any] = {
            "current_count": self.current_count,
            "limit": self.limit,
        }
        if self.known_limits:
            extra["known_limits"] = list(self.known_limits)
        if self.original_error is not None:
            if self.original_error.method_id is not None:
                extra["method_id"] = self.original_error.method_id
            if self.original_error.rpc_code is not None:
                extra["rpc_code"] = self.original_error.rpc_code
        return extra


# =============================================================================
# Domain: Chat
# =============================================================================


class ChatError(NotebookLMError):
    """Base for chat operations."""


class ChatResponseParseError(ChatError):
    """The streaming chat response yielded no parseable chunks.

    Raised when :func:`notebooklm._chat.wire.parse_streaming_chat_response`
    iterates the streamed response and finds zero ``wrb.fr`` envelopes it
    could decode — that is, the wire protocol drifted or the response body
    was empty/malformed.

    This is distinct from "the model returned an empty answer": a real
    empty answer still produces at least one parseable ``wrb.fr`` chunk
    (with empty answer text), in which case the parser returns a
    ``StreamingChatParseResult("", [], conv_id)`` rather than raising.

    Inherits from :class:`ChatError` so existing chat-domain ``except
    ChatError`` clauses continue to catch it without modification.
    """


# =============================================================================
# Domain: Sources (migrated from types.py)
# =============================================================================


class SourceError(NotebookLMError):
    """Base for source operations."""


class SourceAddError(SourceError):
    """Failed to add a source.

    Attributes:
        url: The URL or identifier that failed.
        cause: The underlying exception.
    """

    def __init__(
        self,
        url: str,
        cause: Exception | None = None,
        message: str | None = None,
    ):
        self.url = url
        self.cause = cause
        msg = message or (
            f"Failed to add source: {url}\n"
            "Possible causes:\n"
            "  - URL is invalid or inaccessible\n"
            "  - Content is behind a paywall or requires authentication\n"
            "  - Page content is empty or could not be parsed\n"
            "  - Rate limiting or quota exceeded"
        )
        super().__init__(msg)


class SourceNotFoundError(NotFoundError, RPCError, SourceError):
    """Source not found in notebook.

    Inherits from :class:`NotFoundError` (cross-domain umbrella),
    :class:`RPCError` (transport-level catchability), and :class:`SourceError`
    (domain base). The RPC base is what ``client.sources.get_fulltext`` raises
    (and what ``client.sources.wait_until_ready`` raises during polling when
    the source disappears) when the server returns an empty / degenerate
    payload for a missing source ID, so ``except RPCError`` keeps working at
    call sites that handle transport-level failures. ``except SourceError``
    continues to work at domain-level call sites that don't care about the
    RPC layer. ``except NotFoundError`` catches it alongside
    :class:`NotebookNotFoundError` and :class:`ArtifactNotFoundError`.

    Note that ``client.sources.get`` returns ``None`` for a missing source
    rather than raising — only the workflows that need a concrete source to
    proceed (e.g. ``get_fulltext``, ``wait_until_ready``) surface the missing
    source as an exception.

    .. note::
       **v0.6.0 BREAKING CHANGE:** prior to v0.6.0, :class:`SourceNotFoundError`
       did NOT inherit from :class:`RPCError`. Code that catches ``RPCError``
       *before* a more specific ``except SourceNotFoundError`` clause may now
       intercept what previously fell through to the specific handler. Reorder
       your ``except`` clauses to put the more specific exceptions first. This
       restores symmetry with :class:`NotebookNotFoundError`, which has
       inherited from :class:`RPCError` since the 0.5.x series.

    Attributes:
        source_id: The ID that was not found.
        method_id: The RPC method ID (inherited from :class:`RPCError`).
        raw_response: First 80 chars of the raw response, if any
            (``NOTEBOOKLM_DEBUG=1`` preserves the full body).
    """

    def __init__(
        self,
        source_id: str,
        *,
        method_id: str | None = None,
        raw_response: str | None = None,
    ):
        self.source_id = source_id
        super().__init__(
            f"Source not found: {source_id}",
            method_id=method_id,
            raw_response=raw_response,
        )


class SourceProcessingError(SourceError):
    """Source failed to process.

    Attributes:
        source_id: The ID of the failed source.
        status: The status code (typically 3 for ERROR).
    """

    def __init__(self, source_id: str, status: int = 3, message: str = ""):
        self.source_id = source_id
        self.status = status
        msg = message or f"Source {source_id} failed to process"
        super().__init__(msg)


class SourceTimeoutError(WaitTimeoutError, SourceError):
    """Timed out waiting for source readiness.

    Inherits from :class:`WaitTimeoutError` (and therefore the built-in
    :class:`TimeoutError`) in addition to :class:`SourceError`. The
    ``WaitTimeoutError`` mixin is additive: ``except SourceError`` and the new
    ``except WaitTimeoutError`` / ``except TimeoutError`` clauses all catch it.

    Attributes:
        source_id: The ID of the source.
        timeout: The timeout duration in seconds.
        last_status: The last observed status before timeout.
    """

    def __init__(
        self,
        source_id: str,
        timeout: float,
        last_status: int | None = None,
    ):
        self.source_id = source_id
        self.timeout = timeout
        self.last_status = last_status
        status_info = f" (last status: {last_status})" if last_status is not None else ""
        super().__init__(f"Source {source_id} not ready after {timeout:.1f}s{status_info}")


# =============================================================================
# Domain: Artifacts (migrated from types.py)
# =============================================================================


class ArtifactError(NotebookLMError):
    """Base for artifact operations."""


class ArtifactNotFoundError(NotFoundError, RPCError, ArtifactError):
    """Artifact not found.

    Inherits from :class:`NotFoundError` (cross-domain umbrella),
    :class:`RPCError` (transport-level catchability), and :class:`ArtifactError`
    (domain base). The RPC base is what artifact-download paths raise when the
    listed artifacts do not include the requested ID, so ``except RPCError``
    keeps working at call sites that handle transport-level failures.
    ``except ArtifactError`` continues to work at domain-level call sites that
    don't care about the RPC layer. ``except NotFoundError`` catches it
    alongside :class:`NotebookNotFoundError` and :class:`SourceNotFoundError`.

    .. note::
       **v0.6.0 BREAKING CHANGE:** prior to v0.6.0, :class:`ArtifactNotFoundError`
       did NOT inherit from :class:`RPCError`. Code that catches ``RPCError``
       *before* a more specific ``except ArtifactNotFoundError`` clause may now
       intercept what previously fell through to the specific handler. Reorder
       your ``except`` clauses to put the more specific exceptions first. This
       restores symmetry with :class:`NotebookNotFoundError`, which has
       inherited from :class:`RPCError` since the 0.5.x series.

    Attributes:
        artifact_id: The ID that was not found.
        artifact_type: The type of artifact (e.g., "audio", "video").
        method_id: The RPC method ID (inherited from :class:`RPCError`).
        raw_response: First 80 chars of the raw response, if any
            (``NOTEBOOKLM_DEBUG=1`` preserves the full body).
    """

    def __init__(
        self,
        artifact_id: str,
        artifact_type: str | None = None,
        *,
        method_id: str | None = None,
        raw_response: str | None = None,
    ):
        self.artifact_id = artifact_id
        self.artifact_type = artifact_type
        # ``str.capitalize()`` on a string with a leading space returns the
        # string unchanged (the first character has no uppercase equivalent),
        # so capitalize ``artifact_type`` first and then build the label —
        # this matches the ``ArtifactNotReadyError`` pattern on this file and
        # the ``SourceNotFoundError`` / ``NotebookNotFoundError`` messages.
        type_label = f"{artifact_type.capitalize()} artifact" if artifact_type else "Artifact"
        super().__init__(
            f"{type_label} not found: {artifact_id}",
            method_id=method_id,
            raw_response=raw_response,
        )


class ArtifactNotReadyError(ArtifactError):
    """Artifact not in completed/ready state.

    Attributes:
        artifact_type: The type of artifact.
        artifact_id: The ID (if known).
        status: The current status (if known).
    """

    def __init__(
        self,
        artifact_type: str,
        artifact_id: str | None = None,
        status: str | None = None,
    ):
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.status = status
        if artifact_id:
            msg = f"{artifact_type.capitalize()} artifact {artifact_id} is not ready"
            if status:
                msg += f" (status: {status})"
        else:
            msg = f"No completed {artifact_type} found"
        super().__init__(msg)


class ArtifactParseError(ArtifactError):
    """Artifact data cannot be parsed.

    Attributes:
        artifact_type: The type being parsed.
        artifact_id: The ID (if known).
        details: Additional error details.
        cause: The underlying exception.
    """

    def __init__(
        self,
        artifact_type: str,
        details: str | None = None,
        artifact_id: str | None = None,
        cause: Exception | None = None,
    ):
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.details = details
        self.cause = cause
        msg = f"Failed to parse {artifact_type} artifact"
        if artifact_id:
            msg += f" {artifact_id}"
        if details:
            msg += f": {details}"
        super().__init__(msg)


class ArtifactDownloadError(ArtifactError):
    """Failed to download artifact content.

    Attributes:
        artifact_type: The type being downloaded.
        artifact_id: The ID (if known).
        details: Additional error details.
        cause: The underlying exception.
        status_code: HTTP status code from the failed response, when the
            failure was an HTTP-level error (e.g. 401, 403, 500). ``None`` for
            transport-level failures (timeouts, DNS, connection resets) where
            no response was received.
    """

    def __init__(
        self,
        artifact_type: str,
        details: str | None = None,
        artifact_id: str | None = None,
        cause: Exception | None = None,
        status_code: int | None = None,
    ):
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.details = details
        self.cause = cause
        self.status_code = status_code
        msg = f"Failed to download {artifact_type} artifact"
        if artifact_id:
            msg += f" {artifact_id}"
        if details:
            msg += f": {details}"
        super().__init__(msg)


class ArtifactFeatureUnavailableError(RPCError, ArtifactError):
    """Artifact generation feature is unavailable for this request.

    NotebookLM can accept a ``CREATE_ARTIFACT`` request but return a null
    result when a specific artifact feature is disabled, gated, or rejected
    before a generation task is created. This is not schema drift: the RPC
    decoded successfully, but no task row exists to parse.

    Attributes:
        artifact_type: The artifact type being generated.
        method_id: The RPC method ID (inherited from :class:`RPCError`).
        raw_response: First 80 chars of the raw response, if any
            (``NOTEBOOKLM_DEBUG=1`` preserves the full body).
    """

    def __init__(
        self,
        artifact_type: str,
        *,
        method_id: str | None = None,
        raw_response: str | None = None,
    ):
        self.artifact_type = artifact_type
        super().__init__(
            f"{artifact_type.replace('_', ' ').capitalize()} generation is unavailable",
            method_id=method_id,
            raw_response=raw_response,
        )


class ArtifactTimeoutError(WaitTimeoutError, ArtifactError):
    """Artifact generation did not reach a terminal state before timeout.

    The exception remains catchable as built-in :class:`TimeoutError` for
    backward compatibility (via the :class:`WaitTimeoutError` umbrella, which
    mixes in ``TimeoutError``), and is now also catchable via
    ``except WaitTimeoutError`` alongside source and research wait timeouts,
    while exposing structured fields for callers that need to distinguish
    queued tasks from tasks that started but did not complete.

    Attributes:
        notebook_id: Notebook containing the artifact task.
        task_id: Artifact generation task ID.
        timeout: Wait budget in seconds.
        timeout_seconds: Alias for ``timeout``.
        last_status: Last observed status before timeout.
        status_history: Ordered status strings emitted by the poll loop when
            the status changed.
        status_transitions: Ordered status snapshots emitted by the poll loop
            when the status changed.
        stalled_phase: Coarse phase where the timeout occurred.
    """

    def __init__(
        self,
        notebook_id: str,
        task_id: str,
        timeout: float,
        *,
        last_status: str | None = None,
        status_history: Sequence[str] | None = None,
        status_transitions: Sequence[GenerationStatus] | None = None,
        stalled_phase: ArtifactStalledPhase | None = None,
    ):
        self.notebook_id = notebook_id
        self.task_id = task_id
        self.timeout = timeout
        self.timeout_seconds = timeout
        self.last_status = last_status
        self.status_transitions: tuple[GenerationStatus, ...] = tuple(status_transitions or ())
        if status_history is None:
            status_history = tuple(
                status.status
                for status in self.status_transitions
                if isinstance(getattr(status, "status", None), str)
            )
        self.status_history = tuple(status_history)
        self.stalled_phase: ArtifactStalledPhase | None = stalled_phase

        history = " -> ".join(self.status_history)
        history_info = f"; status history: {history}" if history else ""
        status_info = f"last status: {last_status}" if last_status is not None else "no status"
        super().__init__(
            f"Task {task_id} in notebook {notebook_id} timed out after "
            f"{timeout}s ({status_info}{history_info})"
        )


class ArtifactPendingTimeoutError(ArtifactTimeoutError):
    """Artifact generation timed out before reaching ``in_progress``."""

    def __init__(
        self,
        notebook_id: str,
        task_id: str,
        timeout: float,
        *,
        last_status: str | None = None,
        status_history: Sequence[str] | None = None,
        status_transitions: Sequence[GenerationStatus] | None = None,
    ):
        super().__init__(
            notebook_id,
            task_id,
            timeout,
            last_status=last_status,
            status_history=status_history,
            status_transitions=status_transitions,
            stalled_phase="pending",
        )


class ArtifactInProgressTimeoutError(ArtifactTimeoutError):
    """Artifact generation timed out after reaching ``in_progress``."""

    def __init__(
        self,
        notebook_id: str,
        task_id: str,
        timeout: float,
        *,
        last_status: str | None = None,
        status_history: Sequence[str] | None = None,
        status_transitions: Sequence[GenerationStatus] | None = None,
    ):
        super().__init__(
            notebook_id,
            task_id,
            timeout,
            last_status=last_status,
            status_history=status_history,
            status_transitions=status_transitions,
            stalled_phase="in_progress",
        )


# =============================================================================
# Domain: Research
# =============================================================================


class ResearchError(NotebookLMError):
    """Base for research operations.

    Added in v0.7.0 to give the research domain a catchable base mirroring
    :class:`SourceError` / :class:`ArtifactError`. :class:`ResearchTimeoutError`
    inherits from it (and from :class:`WaitTimeoutError`).

    ``ResearchTaskMismatchError`` deliberately does NOT inherit from this base:
    it remains a :class:`ValidationError` so existing ``except ValidationError``
    clauses on :meth:`ResearchAPI.import_sources` keep catching it unchanged.
    """


class ResearchTimeoutError(WaitTimeoutError, ResearchError):
    """Research task did not reach a terminal state before timeout.

    Raised by :meth:`ResearchAPI.wait_for_completion` when the research task
    does not reach ``completed`` / ``failed`` within the wait budget.

    Inherits from :class:`WaitTimeoutError` (and therefore the built-in
    :class:`TimeoutError`) and :class:`ResearchError`. Before v0.7.0 this path
    raised the bare built-in :class:`TimeoutError`; routing it through this
    subclass is backward-compatible for ``except TimeoutError`` callers and
    newly catchable via ``except WaitTimeoutError`` / ``except ResearchError``.

    Attributes:
        notebook_id: Notebook containing the research task.
        task_id: The research task ID (``"unknown"`` when no task id was
            resolved before the timeout).
        timeout: Wait budget in seconds.
        timeout_seconds: Alias for ``timeout``.
        last_status: Last observed research status before timeout.
    """

    def __init__(
        self,
        notebook_id: str,
        task_id: str,
        timeout: float,
        *,
        last_status: str | None = None,
    ):
        self.notebook_id = notebook_id
        self.task_id = task_id
        self.timeout = timeout
        self.timeout_seconds = timeout
        self.last_status = last_status
        status_info = f" (last status: {last_status})" if last_status is not None else ""
        super().__init__(
            f"Research task {task_id} in notebook {notebook_id} timed out "
            f"after {timeout}s{status_info}"
        )


class ResearchTaskMismatchError(ValidationError):
    """Per-source ``research_task_id`` does not match the caller's ``task_id``.

    Raised by :meth:`ResearchAPI.import_sources` when one of the supplied
    sources carries a ``research_task_id`` that differs from the
    discriminator ``task_id`` passed by the caller. This is the wire-crossing
    bug: the caller intends to import results for task A, but one of the
    source entries was actually discovered under task B. Importing under
    the wrong task would mis-attribute provenance, so this check fails
    loud before any RPC traffic is issued.

    Inherits from :class:`ValidationError` so existing ``except
    ValidationError`` clauses on ``import_sources`` continue to catch it.

    Attributes:
        task_id: The discriminator ``task_id`` passed by the caller.
        source_research_task_id: The ``research_task_id`` carried by the
            offending source dict.
    """

    def __init__(self, *, task_id: str, source_research_task_id: str):
        self.task_id = task_id
        self.source_research_task_id = source_research_task_id
        super().__init__(
            f"research_task_id mismatch: source carries "
            f"research_task_id={source_research_task_id!r} but caller passed "
            f"task_id={task_id!r}. Sources discovered under one research "
            f"task cannot be imported under another."
        )


# =============================================================================
# Domain: Notes
# =============================================================================


class NoteError(NotebookLMError):
    """Base for note operations.

    Gives the note domain a catchable base mirroring :class:`SourceError` /
    :class:`ArtifactError`. :class:`NoteNotFoundError` inherits from it.
    """


class NoteNotFoundError(NotFoundError, RPCError, NoteError):
    """Note not found in notebook.

    .. note::
       Under v0.7.0 this type is raised only when ``NOTEBOOKLM_FUTURE_ERRORS``
       is on — ``notes.get`` (via ``resolve_get``, #1247) and ``notes.update``
       (#1362) then fail loud on a missing note. It is the unconditional
       not-found signal for note paths in v0.8.0 (#1346); see below.

    Inherits from :class:`NotFoundError` (cross-domain umbrella),
    :class:`RPCError` (transport-level catchability), and :class:`NoteError`
    (domain base). The RPC base is what note read/mutation paths will raise when
    the server returns an empty / degenerate payload for a missing note ID, so
    ``except RPCError`` keeps working at call sites that handle transport-level
    failures. ``except NoteError`` works at domain-level call sites that don't
    care about the RPC layer. ``except NotFoundError`` catches it alongside
    :class:`NotebookNotFoundError` and :class:`SourceNotFoundError`.

    Attributes:
        note_id: The ID that was not found.
        method_id: The RPC method ID (inherited from :class:`RPCError`).
        raw_response: First 80 chars of the raw response, if any
            (``NOTEBOOKLM_DEBUG=1`` preserves the full body).
    """

    def __init__(
        self,
        note_id: str,
        *,
        method_id: str | None = None,
        raw_response: str | None = None,
    ):
        self.note_id = note_id
        super().__init__(
            f"Note not found: {note_id}",
            method_id=method_id,
            raw_response=raw_response,
        )


# =============================================================================
# Domain: Mind maps
# =============================================================================


class MindMapError(NotebookLMError):
    """Base for mind-map operations.

    Gives the mind-map domain a catchable base mirroring :class:`SourceError` /
    :class:`ArtifactError`. :class:`MindMapNotFoundError` inherits from it.
    """


class MindMapNotFoundError(NotFoundError, RPCError, MindMapError):
    """Mind map not found in notebook.

    Raised by ``client.mind_maps.rename`` (and the underlying internal
    ``NoteBackedMindMapService.rename_mind_map``) on a missing target
    (issue #1291). Absence is detected via a content/list lookup, not a
    transport 404 (mind maps share storage with notes / studio artifacts). The
    derived read ``get_tree`` and the idempotent ``delete`` interpret the same
    absence signal differently: ``get_tree`` returns ``None`` and ``delete`` is
    a no-op (ADR-0019).

    Inherits from :class:`NotFoundError` (cross-domain umbrella),
    :class:`RPCError` (transport-level catchability), and :class:`MindMapError`
    (domain base), so ``except RPCError`` keeps working at call sites that handle
    transport-level failures. ``except MindMapError`` works at domain-level call
    sites that don't care about the RPC layer. ``except NotFoundError`` catches
    it alongside :class:`NotebookNotFoundError` and :class:`SourceNotFoundError`.

    Attributes:
        mind_map_id: The ID that was not found.
        method_id: The RPC method ID (inherited from :class:`RPCError`).
        raw_response: First 80 chars of the raw response, if any
            (``NOTEBOOKLM_DEBUG=1`` preserves the full body).
    """

    def __init__(
        self,
        mind_map_id: str,
        *,
        method_id: str | None = None,
        raw_response: str | None = None,
    ):
        self.mind_map_id = mind_map_id
        super().__init__(
            f"Mind map not found: {mind_map_id}",
            method_id=method_id,
            raw_response=raw_response,
        )
