"""Shared exponential-backoff helper for retry loops.

A single ``compute_backoff_delay`` is used by every transient-retry path in
the package so the curve, jitter shape, and cap-behavior are defined in one
place. The function is pure math (sync, no I/O) — callers pair it with their
own sleep primitive.

Jitter uses an ``rng`` parameter so tests can pass a seeded ``random.Random``
for deterministic output. In production callers pass ``rng=None`` and the
module's shared ``random`` is used. Setting ``jitter_ratio=0`` short-circuits
the rng call entirely, which both keeps no-jitter call sites bit-exact and
lets them avoid any randomness dependency.
"""

from __future__ import annotations

import random as _random

__all__ = ["compute_backoff_delay"]


def compute_backoff_delay(
    attempt: int,
    base: float = 1.0,
    cap: float = 60.0,
    jitter_ratio: float = 0.1,
    *,
    rng: _random.Random | None = None,
) -> float:
    """Return the next backoff delay in seconds.

    Exponential growth with a cap and bounded multiplicative jitter:

        raw = min(base * 2 ** attempt, cap)
        delay = raw + uniform(-jitter_ratio * raw, +jitter_ratio * raw)

    so the result is always within ``raw * (1 ± jitter_ratio)``.

    ``attempt`` is 0-indexed: ``attempt=0`` yields ``base`` (± jitter),
    ``attempt=1`` yields ``2 * base``, and so on. Negative ``attempt`` is
    treated as 0.

    ``jitter_ratio=0`` produces a deterministic ``raw`` value and skips
    the rng call entirely, so call sites that want bit-exact powers-of-two
    (e.g. existing test schedules) can opt out of randomness cleanly.

    ``rng=None`` falls back to the module's shared ``random`` source.
    Tests should pass ``rng=random.Random(seed)`` for reproducibility.
    """
    if jitter_ratio < 0:
        raise ValueError("jitter_ratio must be non-negative")
    if attempt < 0:
        attempt = 0
    raw = min(base * (2**attempt), cap)
    if jitter_ratio == 0:
        return raw
    source = rng if rng is not None else _random
    spread = jitter_ratio * raw
    return raw + source.uniform(-spread, spread)
