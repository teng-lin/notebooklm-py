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
    "RateLimitError",
    "ServerError",
    "ClientError",
    "RPCTimeoutError",
    # Domain: Notebooks
    "NotebookError",
    "NotebookNotFoundError",
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

    # Backward compatibility aliases
    @property
    def rpc_id(self) -> str | None:
        """Alias for method_id (deprecated, use method_id instead)."""
        return self.method_id


# =============================================================================
# RPC Protocol
# =============================================================================


class RPCError(NotebookLMError):
    """Base for RPC-specific failures after connection established.

    Attributes:
        method_id: The RPC method ID (e.g., "abc123") for debugging.
        raw_response: First 500 chars of raw response for debugging.
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
        self.raw_response = raw_response[:500] if raw_response else None
        self.rpc_code = rpc_code
        self.found_ids = found_ids or []

    # Backward compatibility aliases
    @property
    def rpc_id(self) -> str | None:
        """Alias for method_id (deprecated, use method_id instead)."""
        return self.method_id

    @property
    def code(self) -> str | int | None:
        """Alias for rpc_code (deprecated, use rpc_code instead)."""
        return self.rpc_code

    @property
    def raw_response_preview(self) -> str | None:
        """Alias for raw_response (deprecated, use raw_response instead)."""
        return self.raw_response


class DecodingError(RPCError):
    """Failed to parse RPC response structure.

    This indicates the API returned data in an unexpected format.
    """


class UnknownRPCMethodError(DecodingError):
    """RPC response structure doesn't match expectations.

    This often indicates Google has changed the API. Check for library updates.
    """


class AuthError(RPCError):
    """Authentication or authorization failure.

    Attributes:
        recoverable: True if re-authentication might help (e.g., token expired).
    """

    recoverable: bool = False


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
# Domain: Notebooks
# =============================================================================


class NotebookError(NotebookLMError):
    """Base for notebook operations."""


class NotebookNotFoundError(NotebookError):
    """Notebook not found.

    Attributes:
        notebook_id: The ID that was not found.
    """

    def __init__(self, notebook_id: str):
        self.notebook_id = notebook_id
        super().__init__(f"Notebook not found: {notebook_id}")


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
        msg = f"Failed to download {artifact_type} artifact"
        if artifact_id:
            msg += f" {artifact_id}"
        if details:
            msg += f": {details}"
        super().__init__(msg)
