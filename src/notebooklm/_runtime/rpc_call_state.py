"""Typed per-call state shared by the web runtime pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .._request_types import AuthSnapshot, BuildRequest

if TYPE_CHECKING:
    from .._auth_refresh_retry import RefreshBudget
    from .._deadline import RuntimeDeadline


@dataclass(slots=True)
class _RpcCallProgress:
    """Bounded facts published while one logical call progresses."""

    auth_snapshot: AuthSnapshot | None
    auth_refreshed: bool = False
    retry_attempt: int = 0
    queue_wait_seconds: float | None = None
    response_status_code: int | None = None
    response_error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcCallState:
    """Immutable call configuration with a bounded shared progress record.

    Retry requests retain this exact object. Auth refresh can therefore
    publish a new snapshot once and a later retry of the original request
    rematerializes from the refreshed state without a mutable metadata dict.
    """

    build_request: BuildRequest | None = None
    log_label: str | None = None
    rpc_method: str | None = None
    disable_internal_retries: bool = False
    disable_read_timeout_retries: bool = False
    read_timeout: float | None = None
    max_response_bytes: int | None = None
    refresh_budget: RefreshBudget | None = None
    retry_deadline: RuntimeDeadline | None = None
    _progress: _RpcCallProgress = field(
        default_factory=lambda: _RpcCallProgress(None), repr=False, compare=False
    )

    @classmethod
    def create(
        cls,
        *,
        build_request: BuildRequest | None = None,
        log_label: str | None = None,
        rpc_method: str | None = None,
        disable_internal_retries: bool = False,
        disable_read_timeout_retries: bool = False,
        read_timeout: float | None = None,
        max_response_bytes: int | None = None,
        refresh_budget: RefreshBudget | None = None,
        retry_deadline: RuntimeDeadline | None = None,
        auth_snapshot: AuthSnapshot | None = None,
    ) -> RpcCallState:
        """Build one state while preserving deadline/budget object identity."""
        return cls(
            build_request=build_request,
            log_label=log_label,
            rpc_method=rpc_method,
            disable_internal_retries=disable_internal_retries,
            disable_read_timeout_retries=disable_read_timeout_retries,
            read_timeout=read_timeout,
            max_response_bytes=max_response_bytes,
            refresh_budget=refresh_budget,
            retry_deadline=retry_deadline,
            _progress=_RpcCallProgress(auth_snapshot),
        )

    @property
    def auth_snapshot(self) -> AuthSnapshot | None:
        return self._progress.auth_snapshot

    @property
    def auth_refreshed(self) -> bool:
        return self._progress.auth_refreshed

    @property
    def retry_attempt(self) -> int:
        return self._progress.retry_attempt

    @property
    def queue_wait_seconds(self) -> float | None:
        return self._progress.queue_wait_seconds

    @property
    def response_status_code(self) -> int | None:
        return self._progress.response_status_code

    @property
    def response_error_type(self) -> str | None:
        return self._progress.response_error_type

    def publish_auth_snapshot(self, snapshot: AuthSnapshot, *, refreshed: bool = False) -> None:
        """Publish a captured snapshot to every request for this call."""
        self._progress.auth_snapshot = snapshot
        if refreshed:
            self._progress.auth_refreshed = True

    def mark_auth_refreshed(self) -> None:
        """Record successful once-per-logical-call auth refresh."""
        self._progress.auth_refreshed = True

    def advance_retry_attempt(self) -> int:
        """Increment and return the logical transport retry attempt."""
        self._progress.retry_attempt += 1
        return self._progress.retry_attempt

    def record_queue_wait(self, seconds: float) -> None:
        """Publish semaphore queue latency for the transport recorder."""
        self._progress.queue_wait_seconds = float(seconds)

    def record_response(self, status_code: int) -> None:
        """Publish terminal response diagnostics."""
        self._progress.response_status_code = status_code
        self._progress.response_error_type = None

    def record_failure(self, exc: BaseException) -> None:
        """Publish a scrubbed terminal exception type, never its message."""
        self._progress.response_error_type = type(exc).__qualname__


__all__ = ["RpcCallState"]
