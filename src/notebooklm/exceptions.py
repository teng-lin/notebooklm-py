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
from typing import Any

from ._env import DEFAULT_BASE_URL, get_base_url


def _truncate_response_preview(raw: str | None) -> str | None:
    """Truncate a raw RPC response preview for safe display in error contexts.

    Default behavior keeps the preview compact (80 chars + ``"..."`` suffix) so
    error logs and CLI output stay readable. Set ``NOTEBOOKLM_DEBUG=1`` to opt
    into the full untruncated body for deep debugging.
    """
    if raw is None:
        return None
    if os.environ.get("NOTEBOOKLM_DEBUG") == "1":
        return raw
    if len(raw) > 80:
        return raw[:80] + "..."
    return raw


__all__ = [
    # Base
    "NotebookLMError",
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
    # Idempotency
    "NonIdempotentRetryError",
    # Domain: Notebooks
    "NotebookError",
    "NotebookNotFoundError",
    "NotebookLimitError",
    # Domain: Chat
    "ChatError",
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
    # Domain: Research
    "ResearchTaskMismatchError",
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
        A small number of domain-level exceptions also inherit from
        :class:`RPCError` so that ``except RPCError`` keeps catching them at
        transport-level call sites. Currently :class:`NotebookNotFoundError`
        is one such case — the underlying RPC call succeeded but returned a
        degenerate payload, and historic callers relied on ``except RPCError``
        to handle it. When writing new ``except RPCError`` clauses, be aware
        these domain errors may also flow through; catch the specific domain
        type first if you want to handle it differently.

    Attributes:
        method_id: The RPC method ID (e.g., "abc123") for debugging.
        raw_response: First 80 chars of raw response for debugging
            (with ``"..."`` suffix if truncated). Set ``NOTEBOOKLM_DEBUG=1`` to
            preserve the full body.
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
        """Alias for method_id (deprecated, use method_id instead)."""
        import warnings

        warnings.warn(
            "The 'rpc_id' attribute is deprecated, use 'method_id' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.method_id

    @property
    def code(self) -> str | int | None:
        """Alias for rpc_code (deprecated, use rpc_code instead)."""
        import warnings

        warnings.warn(
            "The 'code' attribute is deprecated, use 'rpc_code' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
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
            (``NOTEBOOKLM_DEBUG=1`` preserves the full body).
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
        # Slice before substituting so we don't run the regex over a multi-MB
        # response body just to throw away most of it. A 5x headroom over
        # PREVIEW_LENGTH guarantees we still have enough non-whitespace
        # characters left after collapsing runs of whitespace, even on heavily
        # indented HTML where ~80% of the prefix may be indentation.
        head = payload_preview[: self.PREVIEW_LENGTH * 5]
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


# =============================================================================
# Domain: Notebooks
# =============================================================================


class NotebookError(NotebookLMError):
    """Base for notebook operations."""


class NotebookNotFoundError(RPCError, NotebookError):
    """Notebook not found.

    Inherits from both :class:`RPCError` and :class:`NotebookError` so callers
    can catch either base. The RPC base is what ``client.notebooks.get`` raises
    when the server returns an empty / degenerate payload for a missing ID, so
    ``except RPCError`` keeps working at call sites that handle transport-level
    failures. ``except NotebookError`` continues to work at domain-level call
    sites that don't care about the RPC layer.

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


class SourceNotFoundError(SourceError):
    """Source not found in notebook.

    Attributes:
        source_id: The ID that was not found.
    """

    def __init__(self, source_id: str):
        self.source_id = source_id
        super().__init__(f"Source not found: {source_id}")


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


class SourceTimeoutError(SourceError):
    """Timed out waiting for source readiness.

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


class ArtifactNotFoundError(ArtifactError):
    """Artifact not found.

    Attributes:
        artifact_id: The ID that was not found.
        artifact_type: The type of artifact (e.g., "audio", "video").
    """

    def __init__(self, artifact_id: str, artifact_type: str | None = None):
        self.artifact_id = artifact_id
        self.artifact_type = artifact_type
        type_info = f" {artifact_type}" if artifact_type else ""
        super().__init__(f"{type_info.capitalize()} artifact {artifact_id} not found")


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


# =============================================================================
# Domain: Research
# =============================================================================


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
