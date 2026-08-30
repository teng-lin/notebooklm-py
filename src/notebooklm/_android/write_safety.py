"""Shared outcome semantics for non-replayed Android mutations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from .._idempotency import mark_unconfirmed
from ..exceptions import NetworkError, RateLimitError, ServerError

T = TypeVar("T")

# These errors can be observed after a request has reached the backend.  They
# therefore say nothing about whether a non-idempotent write committed.  Auth,
# validation, decoding before dispatch, and confirmed 4xx/gRPC rejections are
# deliberately excluded.
AMBIGUOUS_WRITE_ERRORS = (NetworkError, RateLimitError, ServerError)


async def call_unconfirmed_on_transport_loss(call: Callable[[], Awaitable[T]]) -> T:
    """Run one mutation and tag only transport-ambiguous failures."""

    try:
        return await call()
    except AMBIGUOUS_WRITE_ERRORS as error:
        raise mark_unconfirmed(error) from None


__all__ = ["AMBIGUOUS_WRITE_ERRORS", "call_unconfirmed_on_transport_loss"]
