"""Transport-neutral commit evidence and replay decisions."""

from __future__ import annotations

import traceback
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Literal, Protocol, TypeVar

from .exceptions import (
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
)
from .outcomes import CommitState

T = TypeVar("T")


class ReplayGrant(str, Enum):
    """Private semantic permission supplied by the operation owner."""

    REFUSAL_RETRY_AUTHORIZED = "refusal_retry_authorized"
    NO_REPLAY = "no_replay"
    REPLAY_SAFE = "replay_safe"


def replay_allowed(
    exc: BaseException | None,
    *,
    grant: ReplayGrant,
    disabled: bool,
    remaining: float | None,
) -> bool:
    """Return whether canonical evidence and operation semantics permit replay."""
    if disabled or (remaining is not None and remaining <= 0):
        return False
    if grant is ReplayGrant.NO_REPLAY:
        return False
    if grant is ReplayGrant.REPLAY_SAFE:
        return True
    state = getattr(exc, "commit_state", CommitState.UNKNOWN)
    return state in (CommitState.REJECTED, CommitState.NOT_SENT)


# The translated exception types that ``rpc_call`` raises when the
# request fails in a way that *might* have committed the write on the
# server. With ``disable_internal_retries=True``, the middleware retry loop
# inside ``RuntimeTransport.perform_authed_post`` does not replay these;
# instead ``rpc_call`` translates the underlying ``TransportServerError`` /
# network failure into ``ServerError`` / ``NetworkError`` / ``RateLimitError``
# and surfaces it here. Anything else (auth, validation, decoding) propagates
# unchanged unless a producer has attached more precise evidence.
#
# Note: ``RPCTimeoutError`` inherits from ``NetworkError`` so it is
# already covered by the ``NetworkError`` catch.
_RETRYABLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    RateLimitError,
    ServerError,
    NetworkError,
)

AMBIGUOUS_WRITE_ERRORS = _RETRYABLE_TRANSPORT_ERRORS


_E = TypeVar("_E", bound=BaseException)


def mark_commit_state(
    exc: _E,
    state: CommitState,
    *,
    operation: str | None = None,
) -> _E:
    """Attach positive commit evidence without overwriting earlier evidence."""
    current = getattr(exc, "commit_state", None)
    if current is None:
        exc.commit_state = state  # type: ignore[attr-defined]
    if operation is not None and getattr(exc, "operation", None) is None:
        exc.operation = operation  # type: ignore[attr-defined]
    return exc


def mark_unconfirmed(
    exc: _E,
    *,
    force_unknown: bool = False,
    operation: str | None = None,
) -> _E:
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
    current = getattr(exc, "commit_state", None)
    if not force_unknown and current in (
        CommitState.NOT_SENT,
        CommitState.REJECTED,
        CommitState.CONFIRMED,
    ):
        if operation is not None and getattr(exc, "operation", None) is None:
            exc.operation = operation  # type: ignore[attr-defined]
        return exc
    exc.commit_state = CommitState.UNKNOWN  # type: ignore[attr-defined]
    exc.unconfirmed = True  # type: ignore[attr-defined]
    if operation is not None:
        exc.operation = operation  # type: ignore[attr-defined]
    return exc


class _MethodIdentifier(Protocol):
    """Structural method identity shared by web enums and Android strings."""

    @property
    def value(self) -> str: ...


_Method = _MethodIdentifier | str


def _method_id(method: _Method) -> str:
    # ``RPCMethod`` is also a ``str`` subclass. Resolve its enum value before
    # the generic string case so exception metadata never retains an enum
    # instance where callers expect a built-in ``str``.
    value = getattr(method, "value", None)
    return str(value) if isinstance(value, str) else str(method)


def unresolved_commit_error(
    method: _Method,
    what: str,
    exc: _E,
    *,
    preserve_exception: bool = False,
    force_unknown: bool = False,
    operation: str | None = None,
) -> _E | RPCError:
    """Build or tag an error for a write whose commit outcome is unknown.

    ``preserve_exception=True`` explicitly preserves an already-rendered
    domain-specific exception type and guidance. Transport exceptions receive
    the shared generic ``RPCError`` used by web call sites that do not have a
    more specific domain wrapper. Exception text is deliberately not used to
    select between those contracts: upstream transport messages are untrusted.
    """

    if preserve_exception:
        return mark_unconfirmed(exc, force_unknown=force_unknown, operation=operation)

    rpc_code = exc.rpc_code if isinstance(exc, RPCError) else None
    return mark_unconfirmed(
        RPCError(
            f"UNRESOLVED — {what} may have committed before its response was lost. "
            "Do not blindly retry; list the notebook's sources and reconcile first. "
            f"No automatic retry was attempted. {exc}",
            method_id=_method_id(method),
            rpc_code=rpc_code,
        ),
        force_unknown=force_unknown,
        operation=operation,
    )


async def call_unconfirmed_on_transport_loss(
    call: Callable[[], Awaitable[T]],
    *,
    method: _Method,
    what: str,
    chain: Literal["exc"] | None = "exc",
    force_unknown: bool = False,
    operation: str | None = None,
) -> T:
    """Run one non-replayed write and mark transport-loss ambiguity.

    The original exception object, class, and message are preserved. ``method``
    and ``what`` make the write identity explicit at every call site and are
    consumed by guardrails. Web callers retain normal exception context;
    Android callers pass ``chain=None`` so bearer-owning transport frames stay
    outside the escaping exception chain.
    """

    if chain not in ("exc", None):
        raise ValueError("chain must be 'exc' or None")
    failure: BaseException | None = None
    try:
        return await call()
    except AMBIGUOUS_WRITE_ERRORS as exc:
        mark_unconfirmed(exc, force_unknown=force_unknown, operation=operation)
        if chain == "exc":
            del call, method, what
            raise
        failure = exc
    except RPCError as exc:
        if not force_unknown:
            raise
        mark_unconfirmed(exc, force_unknown=True, operation=operation)
        if chain == "exc":
            del call, method, what
            raise
        failure = exc

    assert failure is not None
    captured = failure.__traceback__
    failure.__traceback__ = None
    failure.__cause__ = None
    failure.__context__ = None
    failure.__suppress_context__ = True
    completed = captured
    while (
        completed is not None
        and completed.tb_frame.f_code is call_unconfirmed_on_transport_loss.__code__
    ):
        completed = completed.tb_next
    if completed is not None:
        traceback.clear_frames(completed)
    del call, method, what
    del captured, completed
    raise failure from None


__all__ = [
    "AMBIGUOUS_WRITE_ERRORS",
    "ReplayGrant",
    "call_unconfirmed_on_transport_loss",
    "mark_commit_state",
    "mark_unconfirmed",
    "replay_allowed",
    "unresolved_commit_error",
]
