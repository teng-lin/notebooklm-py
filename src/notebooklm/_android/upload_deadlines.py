"""Android transfer timeout normalization and aggregate derivation."""

from __future__ import annotations

import math

import httpx


def _resolve_upload_timeouts(
    configured: httpx.Timeout | float | None,
) -> tuple[float, httpx.Timeout | None]:
    """Resolve the legacy upload timeout into aggregate and HTTP budgets."""

    if configured is None:
        return 300.0, None
    if isinstance(configured, httpx.Timeout):
        components = [
            component
            for component in (
                configured.connect,
                configured.read,
                configured.write,
                configured.pool,
            )
            if component is not None
        ]
        for component in components:
            if not math.isfinite(float(component)) or float(component) <= 0.0:
                raise ValueError("upload_timeout components must be finite positive numbers")
        aggregate = 300.0 if not components else max(300.0, 2.0 * sum(components))
        return aggregate, configured
    numeric = float(configured)
    if not math.isfinite(numeric) or numeric <= 0.0:
        raise ValueError("upload_timeout must be a finite positive number")
    return numeric, None


def _component_sum(configured: httpx.Timeout | None) -> float:
    """Sum the finite components used to derive a phase aggregate."""

    if configured is None:
        return 0.0
    components = tuple(
        float(component)
        for component in (
            configured.connect,
            configured.read,
            configured.write,
            configured.pool,
        )
        if component is not None
    )
    if any(not math.isfinite(component) or component <= 0.0 for component in components):
        raise ValueError("upload_timeout components must be finite positive numbers")
    return sum(components)


__all__ = ["_component_sum", "_resolve_upload_timeouts"]
