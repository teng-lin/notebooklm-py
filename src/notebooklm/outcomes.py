"""Public operation-outcome evidence shared across backends.

Commit certainty is deliberately independent from an exception's category.
Callers may use this enum to distinguish a verified refusal from a response
loss without relying on HTTP/gRPC status codes or exception classes.
"""

from __future__ import annotations

from enum import Enum


class CommitState(str, Enum):
    """Evidence about whether one mutation reached a committed outcome."""

    NOT_SENT = "not_sent"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"


__all__ = ["CommitState"]
