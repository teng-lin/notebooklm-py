"""Transport-neutral probe-then-retry helpers for mutating create workflows.

:func:`idempotent_create` wraps create-RPC patterns where the server may have
committed a write even though the client observed a transport failure. The
wrapper probes for the server-side commit before it permits another create.

Per-API probes used by :func:`idempotent_create` are caller-supplied
because there is no universal probe key (notebooks: title +
baseline-diff; ``add_url``: url-match + baseline-diff; ``add_drive``:
Drive ``documentId``-match + baseline-diff; ``add_text``: no probe
possible — see :class:`~notebooklm.exceptions.NonIdempotentRetryError`).

The web executor's RPC classification registry is intentionally separate in
``notebooklm._web.policy``. This module has no dependency on the web backend or
the RPC method vocabulary.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from .exceptions import (
    NetworkError,
    RateLimitError,
    ServerError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class _CreateResultKind(str, Enum):
    """How an idempotent create obtained its result."""

    CREATED = "created"
    PROBED = "probed"


@dataclass(frozen=True)
class _IdempotentCreateResult(Generic[T]):
    """Private provenance carrier for idempotent create callers."""

    value: T
    kind: _CreateResultKind


def _coerce_create_result(value: T | _IdempotentCreateResult[T]) -> _IdempotentCreateResult[T]:
    """Attach fresh-create provenance to a legacy value-only result."""
    if isinstance(value, _IdempotentCreateResult):
        return value
    return _IdempotentCreateResult(value=value, kind=_CreateResultKind.CREATED)


# The translated exception types that ``rpc_call`` raises when the
# request fails in a way that *might* have committed the write on the
# server. With ``disable_internal_retries=True``, the middleware retry loop
# inside ``RuntimeTransport.perform_authed_post`` does not replay these;
# instead ``rpc_call`` translates the underlying ``TransportServerError`` /
# network failure into ``ServerError`` / ``NetworkError`` / ``RateLimitError``
# and surfaces it here. ``idempotent_create`` catches exactly these; anything else (auth,
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


_E = TypeVar("_E", bound=BaseException)


def mark_unconfirmed(exc: _E) -> _E:
    """Tag an error as *"the write may have committed and we cannot confirm it"*.

    Raised by a probe that could not answer (#2220). This is a genuinely
    distinct outcome from both "the create was rejected" and "the create
    failed", and consumers must be able to tell it apart **programmatically** —
    the two mistakes it prevents are concrete:

    "Could not answer" covers every way a probe fails to settle the question,
    not just an exception while listing. All of these carry the marker:

    * the probe's list raised — a decode failure (wrapped) or a transport /
      auth failure (re-raised unchanged, marker set on the original);
    * the probe listed fine but found a match it **cannot attribute**, because
      the pre-create baseline was unavailable;
    * the probe found **several** new matches and cannot choose;
    * a create RPC returned success but with no trustworthy id, and the
      recovery probe then failed or found nothing unambiguous.

    The last three are the easy ones to miss: nothing threw, so they look like
    ordinary rejections — but the server may hold a row either way, which is
    exactly the state this marker names.

    * ``_app.errors`` classifies a :class:`SourceAddError` by inspecting its
      ``cause``, and a bare ``RPCError`` cause carrying a 5xx / gRPC-14
      ``rpc_code`` maps to :attr:`~notebooklm._app.errors.ErrorCategory.SERVER`
      — *retriable*, hint "retry after a short delay". A probe's own decode
      failure can carry exactly such a code, which would advertise "please
      retry" for the one error whose entire message says the create must not be
      retried. That is the duplicate this whole change prevents, re-introduced
      one layer up.
    * A batch add isolates non-fatal per-item errors and continues. An
      unconfirmed create must instead stop the batch, or a drifted backend turns
      one unconfirmed write into one per item.

    Read it back with ``getattr(exc, "unconfirmed", False)`` — a plain literal
    at the call site, matching how ``source_id`` / ``stage`` are read after
    ``raise_partial_upload_failure`` (#2179). A shared constant was tried and
    rejected: it belongs on the public exception surface for ``_app`` to import
    (the ``_app`` boundary guardrail forbids reaching into private runtime
    siblings), and putting it there pushed ``exceptions.py`` past its
    module-size ratchet for a single string.

    Set as an attribute on the real exception rather than introducing a wrapper
    or sibling type — the same shape ``raise_partial_upload_failure`` uses for
    ``source_id`` / ``stage``, and for the same reason (#2179): a new type in the
    hierarchy silently changes which ``except`` clauses match at existing call
    sites. Every ``except SourceAddError`` / ``except RPCError`` keeps matching
    exactly as before; only code that asks for the marker sees a difference.
    """
    exc.unconfirmed = True  # type: ignore[attr-defined]
    return exc


async def idempotent_create(
    create: Callable[[], Awaitable[T]],
    probe: Callable[[], Awaitable[T | None]],
    *,
    max_attempts: int = 2,
    label: str = "create",
) -> _IdempotentCreateResult[T]:
    """Probe-then-retry wrapper for mutating create RPCs.

    Args:
        create: Coroutine factory that issues the create RPC. The
            underlying ``rpc_call`` MUST be invoked with
            ``disable_internal_retries=True`` so the first transport
            failure surfaces to this wrapper instead of being replayed
            blindly by the retry middleware inside
            ``RuntimeTransport.perform_authed_post``.
        probe: Coroutine factory that returns the resource if it
            already exists server-side, or ``None`` if not. Probes are
            API-specific (notebooks: list-then-baseline-diff by title;
            ``add_url``: list-then-url-match and ``add_drive``:
            list-then-documentId-match, both filtered by a pre-create
            baseline).

            **A probe must return ``None`` only when it has affirmatively
            established that no matching resource exists.** ``None`` is
            read here as evidence that the create did not land, and it is
            acted on by re-issuing that create. A probe that cannot answer
            — its own list failed, a match it cannot attribute, several
            matches it cannot choose between — must raise instead (#2220).
            Raising aborts the retry loop and surfaces to the caller. A probe
            that wraps its own failure (all four do) yields
            ``__cause__`` = that failure and ``__context__.__context__`` =
            the transport error, since the wrap happens inside the probe's own
            ``except``; a probe that raises directly puts the transport error
            at ``__context__``.

            The alternative, swallowing and returning ``None``, silently
            converts a ``PROBE_THEN_CREATE`` operation into an
            at-least-once one at the moment its guarantee matters most.
            The web policy's ``AT_LEAST_ONCE_ACCEPTED`` classification exists
            for callers who want that, and it is opt-in by name.
        max_attempts: Maximum total ``create()`` invocations (default
            2 — one initial + one retry). Each attempt is followed by
            a probe; the probe runs only after a transport failure.
        label: Diagnostic label embedded in log messages.

    Returns:
        A private result carrying the value and whether it came from a
        successful create or a probe match. Callers must unwrap ``value``
        before returning it from a public API.

    Raises:
        Whatever ``create()`` raises on the final attempt if the probe
        consistently returns ``None`` and retries are exhausted. Non-
        transport exceptions (auth, validation, decoding) propagate
        from the first ``create()`` call without invoking the probe.

        Whatever ``probe()`` raises, immediately and without a further
        create attempt. The probe is awaited inside the handler for the
        transport failure, so that failure is always reachable through the
        ``__context__`` chain and the traceback shows both halves: the
        create that may have committed, and the probe that could not say
        whether it did. Its exact depth depends on the probe — one level
        (``__context__``) when the probe re-raises directly, two when the
        probe wraps its own failure first, as all four in-tree probes do.

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
            return _IdempotentCreateResult(value=await create(), kind=_CreateResultKind.CREATED)
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
                return _IdempotentCreateResult(value=existing, kind=_CreateResultKind.PROBED)
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


__all__ = ["idempotent_create", "mark_unconfirmed"]
