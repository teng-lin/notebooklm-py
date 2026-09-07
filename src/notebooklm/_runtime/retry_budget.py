"""Shared retry counters for one logical RPC."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RetryBudget:
    """Mutable 429 and server-error counters shared across chain re-entry."""

    rate_limit_retries: int = 0
    server_error_retries: int = 0


__all__ = ["RetryBudget"]
