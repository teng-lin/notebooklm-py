"""Common private implementations for public NotebookLM types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import httpx


class UnknownTypeWarning(UserWarning):
    """Emitted when encountering unrecognized type codes from Google API.

    This warning indicates the API returned a type code that this version
    of notebooklm-py doesn't recognize. Consider updating to the latest version.
    """


@dataclass(frozen=True)
class ConnectionLimits:
    """HTTP connection-pool tuning for the underlying httpx transport.

    Wraps the subset of ``httpx.Limits`` we expose so the public API
    doesn't leak the httpx type directly (and stays stable across httpx
    minor versions). Defaults are sized for the typical batchexecute
    fan-out: a few dozen concurrent RPCs against a single host with
    keep-alives held for the duration of an interactive session.

    Constraint: ``max_concurrent_rpcs`` must satisfy
    ``max_concurrent_rpcs <= max_connections`` - otherwise the
    semaphore lets requests through that the pool can't fulfill.
    The constructor for ``NotebookLMClient`` enforces this when both
    are set.
    """

    max_connections: int = 100
    """Hard cap on total concurrent connections in the pool."""

    max_keepalive_connections: int = 50
    """Cap on idle connections held open between requests."""

    keepalive_expiry: float = 30.0
    """Seconds an idle connection stays in the pool before being closed."""

    def to_httpx_limits(self) -> httpx.Limits:
        """Map to ``httpx.Limits`` (lazy import to keep common types dep-light)."""
        import httpx

        return httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=self.max_keepalive_connections,
            keepalive_expiry=self.keepalive_expiry,
        )


@dataclass(frozen=True)
class RpcTelemetryEvent:
    """One logical RPC completion event emitted by ``NotebookLMClient``.

    The event is intentionally backend-agnostic: applications can forward it
    to Prometheus, OpenTelemetry, logs, or a custom counter without this
    package taking a dependency on any metrics framework.
    """

    method: str
    status: Literal["success", "error"]
    elapsed_seconds: float
    request_id: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class ClientMetricsSnapshot:
    """Cumulative in-process observability counters for a client instance."""

    rpc_calls_started: int = 0
    rpc_calls_succeeded: int = 0
    rpc_calls_failed: int = 0
    rpc_rate_limit_retries: int = 0
    rpc_server_error_retries: int = 0
    rpc_auth_retries: int = 0
    rpc_latency_seconds_total: float = 0.0
    rpc_queue_wait_seconds_total: float = 0.0
    rpc_queue_wait_seconds_max: float = 0.0
    upload_queue_wait_seconds_total: float = 0.0
    upload_queue_wait_seconds_max: float = 0.0
    lock_wait_seconds_total: float = 0.0
    lock_wait_seconds_max: float = 0.0


@dataclass(frozen=True)
class AccountLimits:
    """Account-level limits returned by NotebookLM user settings."""

    notebook_limit: int | None = None
    source_limit: int | None = None
    raw_limits: tuple[Any, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AccountTier:
    """Raw NotebookLM tier metadata returned by the homepage tier RPC."""

    tier: str | None = None
    plan_name: str | None = None


@dataclass(frozen=True)
class CitedSourceSelection:
    """Result of applying cited-only filtering to research sources."""

    sources: list[dict[str, Any]]
    cited_url_count: int
    matched_url_source_count: int
    used_fallback: bool = False


def _datetime_from_timestamp(
    value: Any, *, datetime_type: type[datetime] = datetime
) -> datetime | None:
    """Convert an API seconds timestamp to ``datetime``, returning ``None`` if invalid."""
    try:
        return datetime_type.fromtimestamp(value)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
