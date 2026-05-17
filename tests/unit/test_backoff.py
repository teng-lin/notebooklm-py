"""Tests for ``notebooklm._backoff.compute_backoff_delay``.

The helper is pure math; these tests cover the invariants the call sites
rely on (monotonic exponential growth, bounded jitter, hard cap) and the
``rng`` injection seam used by deterministic regression tests.
"""

from __future__ import annotations

import random

import pytest

from notebooklm._backoff import compute_backoff_delay

# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_seeded_rng_is_reproducible():
    """Two calls with seed-equal RNGs must return identical sequences."""
    rng_a = random.Random(42)
    rng_b = random.Random(42)
    seq_a = [compute_backoff_delay(n, rng=rng_a) for n in range(6)]
    seq_b = [compute_backoff_delay(n, rng=rng_b) for n in range(6)]
    assert seq_a == seq_b


def test_zero_jitter_skips_rng_and_returns_exact_power_of_two():
    """jitter_ratio=0 must short-circuit the rng entirely."""

    class _ExplodingRng:
        def uniform(self, low: float, high: float) -> float:  # pragma: no cover
            raise AssertionError("rng must not be consulted when jitter_ratio is 0")

    for n in range(5):
        delay = compute_backoff_delay(
            n,
            base=1.0,
            cap=8.0,
            jitter_ratio=0.0,
            rng=_ExplodingRng(),  # type: ignore[arg-type]
        )
        assert delay == min(2**n, 8.0)


# ---------------------------------------------------------------------------
# Jitter bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attempt", range(8))
def test_delay_within_jitter_band(attempt: int):
    """delay ∈ [raw * (1 - jitter_ratio), raw * (1 + jitter_ratio)]."""
    base, cap, jitter_ratio = 1.0, 30.0, 0.2
    raw = min(base * 2**attempt, cap)
    lower = raw * (1 - jitter_ratio)
    upper = raw * (1 + jitter_ratio)
    rng = random.Random(2026)
    # 200 samples per attempt to exercise both extremes of uniform().
    for _ in range(200):
        delay = compute_backoff_delay(
            attempt,
            base=base,
            cap=cap,
            jitter_ratio=jitter_ratio,
            rng=rng,
        )
        assert lower <= delay <= upper, (delay, lower, upper)


def test_jitter_uses_module_random_when_rng_is_none(monkeypatch):
    """rng=None must consult the module-level ``random`` source.

    This is the production contract: the 3 transport call sites all pass
    ``rng=None`` and rely on the shared ``random`` module both for default
    behavior and for test monkeypatching.
    """
    calls: list[tuple[float, float]] = []

    def _fake_uniform(low: float, high: float) -> float:
        calls.append((low, high))
        return 0.0

    monkeypatch.setattr("notebooklm._backoff._random.uniform", _fake_uniform)
    delay = compute_backoff_delay(2, base=1.0, cap=30.0, jitter_ratio=0.2)
    # raw = 4.0; jitter = 0.0; delay = 4.0
    assert delay == 4.0
    assert calls == [(-0.8, 0.8)]


# ---------------------------------------------------------------------------
# Monotonicity (median behavior, since jitter can briefly invert adjacent samples)
# ---------------------------------------------------------------------------


def test_raw_curve_is_monotonic_then_flat_at_cap():
    """With jitter_ratio=0, the curve is strictly increasing until it caps."""
    base, cap = 1.0, 30.0
    series = [compute_backoff_delay(n, base=base, cap=cap, jitter_ratio=0.0) for n in range(8)]
    # 1, 2, 4, 8, 16, 30, 30, 30
    assert series == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_jittered_curve_is_monotonic_in_expectation():
    """Averaging out jitter, the curve must still grow until the cap.

    Per-sample monotonicity is not guaranteed (jitter can invert adjacent
    points), but the mean across many trials must.
    """
    base, cap, jitter_ratio = 1.0, 30.0, 0.2
    rng = random.Random(9)
    n_samples = 500
    means: list[float] = []
    for attempt in range(7):
        total = sum(
            compute_backoff_delay(
                attempt,
                base=base,
                cap=cap,
                jitter_ratio=jitter_ratio,
                rng=rng,
            )
            for _ in range(n_samples)
        )
        means.append(total / n_samples)
    # 1, 2, 4, 8, 16 strict; then 30, 30 cap (32→30, 64→30).
    for prev, nxt in zip(means[:5], means[1:6], strict=True):
        assert nxt > prev, (means, prev, nxt)
    # After cap, means converge.
    assert abs(means[5] - means[6]) < 1.0


# ---------------------------------------------------------------------------
# Cap behavior
# ---------------------------------------------------------------------------


def test_cap_bounds_raw_growth():
    """Without jitter, the value is clamped to ``cap`` for large ``attempt``."""
    # 2**20 = ~1e6 would dwarf any reasonable cap.
    delay = compute_backoff_delay(20, base=1.0, cap=30.0, jitter_ratio=0.0)
    assert delay == 30.0


def test_cap_applied_before_jitter_so_max_is_bounded():
    """delay ≤ cap * (1 + jitter_ratio) even when raw would explode."""
    cap, jitter_ratio = 30.0, 0.2
    rng = random.Random(3)
    for _ in range(50):
        delay = compute_backoff_delay(
            20,
            base=1.0,
            cap=cap,
            jitter_ratio=jitter_ratio,
            rng=rng,
        )
        assert delay <= cap * (1 + jitter_ratio)
        assert delay >= cap * (1 - jitter_ratio)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_negative_attempt_clamps_to_zero():
    """Defensive: negative attempts shouldn't panic; treat as attempt=0."""
    assert compute_backoff_delay(-1, base=1.0, cap=30.0, jitter_ratio=0.0) == 1.0
    assert compute_backoff_delay(-100, base=2.5, cap=30.0, jitter_ratio=0.0) == 2.5


def test_negative_jitter_ratio_rejected():
    """Contract guard: jitter_ratio is a non-negative spread percentage."""
    with pytest.raises(ValueError, match="jitter_ratio must be non-negative"):
        compute_backoff_delay(0, jitter_ratio=-0.1)


def test_attempt_zero_returns_base_without_jitter():
    assert compute_backoff_delay(0, base=1.0, cap=30.0, jitter_ratio=0.0) == 1.0
    assert compute_backoff_delay(0, base=3.0, cap=30.0, jitter_ratio=0.0) == 3.0


def test_call_site_artifact_polling_matches_legacy_min_powers_of_two():
    """Pin the artifact_polling call-site curve (no jitter, cap=8)."""
    # poll_retry_count is 1-indexed at the call site; verify 1..N produces
    # the expected pre-extraction sequence.
    out = [compute_backoff_delay(n, base=1.0, cap=8.0, jitter_ratio=0.0) for n in range(1, 6)]
    assert out == [2.0, 4.0, 8.0, 8.0, 8.0]


def test_call_site_transport_matches_legacy_with_zero_jitter(monkeypatch):
    """Pin the _core_transport curve with jitter monkeypatched to 0."""
    monkeypatch.setattr("notebooklm._backoff._random.uniform", lambda a, b: 0.0)
    out = [compute_backoff_delay(n, base=1.0, cap=30.0, jitter_ratio=0.2) for n in range(7)]
    # 1, 2, 4, 8, 16, 30, 30
    assert out == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
