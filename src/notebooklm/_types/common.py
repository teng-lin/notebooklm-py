"""Common private implementations for public NotebookLM types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from .research import ResearchSourceInput

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
    # Appended at the END so no existing positional parameter shifts — the
    # public ``ClientMetricsSnapshot`` is constructed positionally in places,
    # and inserting mid-list would be a breaking signature change (the
    # api-compat audit flags a moved positional parameter). Keep new counters
    # here at the tail.
    rpc_decode_errors: int = 0
    """Schema-drift failures surfaced at the **RPC executor's response-decode
    boundary**.

    Bumped whenever the executor rejects a decoded RPC response as schema
    drift — a wrapped shape-drift error (bad JSON / missing key-or-index) or a
    surfaced ``DecodingError`` / ``UnknownRPCMethodError`` raised while decoding
    the response envelope (``safe_index`` inside the decoder). Wire-schema drift
    is the stated #1 breakage class, so this counter separates "Google reshaped
    a response" from an ordinary 5xx / network failure (which lands in
    ``rpc_calls_failed`` via the transport-leg ``MetricsMiddleware``). A decode
    error recovered by a refresh-and-retry is NOT counted; only the error that
    ultimately surfaces is.

    Scope note: this covers drift detected at the executor boundary. Positional
    drift raised *later* by feature-layer ``safe_index`` navigation (after
    ``rpc_call`` returns — e.g. ``_extract_summary``) propagates straight to the
    caller and is not routed through this counter yet; broadening the counting
    boundary to those sites is tracked as a follow-up.
    """


@dataclass(frozen=True)
class AccountLimits:
    """Account-level limits returned by NotebookLM user settings."""

    notebook_limit: int | None = None
    source_limit: int | None = None
    raw_limits: tuple[Any, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CitedSourceSelection:
    """Result of applying cited-only filtering to research sources."""

    sources: list[ResearchSourceInput]
    cited_url_count: int
    matched_url_source_count: int
    used_fallback: bool = False


def _datetime_from_timestamp(value: Any) -> datetime | None:
    """Convert an API seconds timestamp to a UTC ``datetime``, ``None`` if invalid.

    Pinning ``tz=timezone.utc`` makes the result tz-aware and host-independent:
    a naive ``fromtimestamp(value)`` would render in the host's local zone, so the
    same epoch surfaced as a different wall-time string per CI runner / user box and
    mis-stated the absolute instant. ``.timestamp()`` round-trips identically either
    way, so internal sort/dedup/download ordering is unaffected — only the rendered
    string changes (now offset-aware and identical everywhere).
    """
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
