"""Idempotency wrapper for create-RPC patterns.

A create RPC like ``NotebooksAPI.create`` or ``SourcesAPI.add_url`` is a
mutating POST: the *server may have committed the write* even if the
client sees a 5xx or network error. Naive retries duplicate the
resource. This helper inverts the retry direction:

  1. Run ``create()`` with internal-retries disabled.
  2. If ``create()`` raises a retryable transport error (5xx / 429 /
     ``RequestError``), call ``probe()`` to ask the server "did the
     write land anyway?" If yes, return the existing resource. If no,
     retry the create.
  3. After ``max_attempts`` of (create + probe), give up and re-raise.

Per-API probes are caller-supplied because there is no universal probe
key (notebooks: title + baseline-diff; sources: url-match;
``add_text``: no probe possible — see ``NonIdempotentRetryError``).

This module is private (``_idempotency.py``) — call sites live in the
domain APIs (``_notebooks.py``, ``_sources.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .exceptions import NetworkError, RateLimitError, ServerError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The translated exception types that ``rpc_call`` raises when the
# request fails in a way that *might* have committed the write on the
# server. With ``disable_internal_retries=True``, ``_perform_authed_post``
# does not retry these on its own; instead it lets ``rpc_call`` translate
# the underlying ``_TransportServerError``/network failure into
# ``ServerError`` / ``NetworkError`` / ``RateLimitError`` and surface it
# here. ``idempotent_create`` catches exactly these; anything else (auth,
# validation, decoding) propagates unchanged because it indicates the
# request never reached a state where the write could land.
#
# Note: ``RPCTimeoutError`` inherits from ``NetworkError`` so it is
# already covered by the ``NetworkError`` catch.
_RETRYABLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    RateLimitError,
    ServerError,
    NetworkError,
)


async def idempotent_create(
    create: Callable[[], Awaitable[T]],
    probe: Callable[[], Awaitable[T | None]],
    *,
    max_attempts: int = 2,
    label: str = "create",
) -> T:
    """Probe-then-retry wrapper for mutating create RPCs.

    Args:
        create: Coroutine factory that issues the create RPC. The
            underlying ``rpc_call`` MUST be invoked with
            ``disable_internal_retries=True`` so the first transport
            failure surfaces to this wrapper instead of being retried
            blindly inside ``_perform_authed_post``.
        probe: Coroutine factory that returns the resource if it
            already exists server-side, or ``None`` if not. Probes are
            API-specific (notebooks: list-then-baseline-diff by title;
            sources: list-then-url-match).
        max_attempts: Maximum total ``create()`` invocations (default
            2 — one initial + one retry). Each attempt is followed by
            a probe; the probe runs only after a transport failure.
        label: Diagnostic label embedded in log messages.

    Returns:
        The result of a successful ``create()`` call, or the value
        returned by ``probe()`` after a transient transport failure.

    Raises:
        Whatever ``create()`` raises on the final attempt if the probe
        consistently returns ``None`` and retries are exhausted. Non-
        transport exceptions (auth, validation, decoding) propagate
        from the first ``create()`` call without invoking the probe.

    Cancellation:
        Pure ``await`` — no ``asyncio.shield``. A ``CancelledError``
        propagates immediately at the next yield point so the caller
        keeps full structured-concurrency semantics.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    last_error: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await create()
        except _RETRYABLE_TRANSPORT_ERRORS as exc:
            last_error = exc
            logger.warning(
                "%s attempt %d/%d failed with transport error (%s); "
                "probing for server-side commit before retry",
                label,
                attempt,
                max_attempts,
                type(exc).__name__,
            )
            existing = await probe()
            if existing is not None:
                logger.info(
                    "%s probe found existing resource after transport "
                    "failure on attempt %d; returning it without retry",
                    label,
                    attempt,
                )
                return existing
            # Probe returned None: the create did not land. Loop and
            # retry as long as we have attempts remaining.
            logger.debug(
                "%s probe returned no match on attempt %d; will retry create",
                label,
                attempt,
            )

    # Exhausted attempts. Re-raise the last transport error so callers
    # see the original failure, not a synthetic wrapper.
    assert last_error is not None  # loop body always sets this on failure
    logger.error(
        "%s failed after %d attempts with no probe match; re-raising last error",
        label,
        max_attempts,
    )
    raise last_error
