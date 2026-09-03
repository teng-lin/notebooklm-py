"""Transport-neutral auth refresh-and-retry helpers.

The web HTTP and decoded-RPC retry layers and the Android gRPC session share
this once-per-logical-call budget and refresh body.  Callers retain ownership
of their protocol-specific trigger and terminal exception shape.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .._client_metrics import ClientMetrics
    from .._deadline import RuntimeDeadline


class RefreshBudget:
    """Single-consume token bounding a logical call to one auth refresh."""

    __slots__ = ("_available",)

    def __init__(self) -> None:
        self._available = True

    @property
    def available(self) -> bool:
        """Whether the refresh token remains available."""

        return self._available

    def consume(self) -> bool:
        """Claim the token, returning true only for its first consumer."""

        if not self._available:
            return False
        self._available = False
        return True


async def refresh_and_count(
    *,
    refresh: Callable[[], Awaitable[object]],
    on_refresh_failure: Callable[[Exception], BaseException],
    sleep: Callable[[float], Awaitable[object]],
    refresh_retry_delay: float,
    log_label: str,
    logger: logging.Logger,
    metrics: ClientMetrics | None,
    retry_deadline: RuntimeDeadline | None = None,
) -> None:
    """Refresh once, apply the bounded retry delay, and record the retry."""

    logger.info("%s auth error detected, attempting token refresh", log_label)
    try:
        await refresh()
    except Exception as refresh_error:
        logger.warning("Token refresh failed: %s", refresh_error)
        raise on_refresh_failure(refresh_error) from refresh_error

    effective_delay = (
        refresh_retry_delay
        if retry_deadline is None
        else retry_deadline.clamp_sleep(refresh_retry_delay)
    )
    if effective_delay > 0:
        await sleep(effective_delay)
    logger.info("Token refresh successful, retrying %s", log_label)
    if metrics is not None:
        metrics.increment(rpc_auth_retries=1)


__all__ = ["RefreshBudget", "refresh_and_count"]
