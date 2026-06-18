"""Regenerable-baseline registry (ADR-0022).

A *baseline* is a committed snapshot of a value the code already derives
(e.g. ``notebooklm.types.__all__``). Each :class:`~tests._baselines.registry.Baseline`
knows how to (a) derive the value from live code, (b) where its committed JSON
file lives, and (c) how to serialize it. The freeze tests in
``tests/_guardrails/test_public_surface_manifest.py`` load the committed file and
assert it equals ``derive()``; a dev-only ``--update-baselines`` pytest flag
rewrites the file from ``derive()`` instead.

This package is ``_``-prefixed (not a ``test_*`` module) so the freeze tests can
import the registry without tripping the no-cross-test-import gate
(``tests/_guardrails/test_no_cross_test_imports.py``).
"""

from __future__ import annotations

from tests._baselines.registry import BASELINES, Baseline

__all__ = ["BASELINES", "Baseline"]
