"""Deprecated shim — captured-cookie validation now lives in ``psidts_recovery.py``.

ADR-0033's load-composition merge folded this module into
:mod:`notebooklm._auth.psidts_recovery`. The split existed only so the recovery
module stayed under the ADR-0008 module-size budget (its own comment said so),
and it cost a two-node cycle: this module imported ``psidts_recovery`` at module
scope while ``psidts_recovery.validate_with_recovery`` was a 4-line pass-through
that lazily imported back. Both halves now sit in one module, the pass-through
is gone, and ``validate`` / ``heal`` / ``validate_with_recovery`` are that
module's public seam. Every name below is a re-export of its new home; nothing
is defined here.

.. warning::
   **Removal: the next major release.** This shim exists only so an out-of-tree
   importer of ``notebooklm._auth.browser_cookie_recovery`` (a private module, so
   this is already outside the supported surface) does not break mid-minor. Every
   in-tree consumer — ``src/``, ``tests/``, ``scripts/`` — was migrated to
   ``notebooklm._auth.psidts_recovery`` in the same PR that created this file.
   Import from :mod:`notebooklm._auth.psidts_recovery`, or better, from the
   ``notebooklm.auth`` facade, which re-exports ``validate_with_recovery``.
"""

from __future__ import annotations

from .psidts_recovery import (
    ValidationResult,
    heal,
    validate,
    validate_with_recovery,
)

__all__ = [
    "ValidationResult",
    "heal",
    "validate",
    "validate_with_recovery",
]
