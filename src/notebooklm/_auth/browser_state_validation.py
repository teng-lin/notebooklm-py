"""Deprecated shim — the captured-state heal now lives in ``browser_capture.py``.

ADR-0033's browser-cluster merge (PR 4.1) folded this module into
:mod:`notebooklm._auth.browser_capture`. It existed only to keep the capture core
under the ADR-0008 module-size budget: both of its callers were already inside
``browser_capture``, so it failed the deletion test standalone. The name below is
a re-export of its new home; nothing is defined here.

.. warning::
   **Removal: the next major release.** This shim exists only so an out-of-tree
   importer of ``notebooklm._auth.browser_state_validation`` (a private module, so
   this is already outside the supported surface) does not break mid-minor. Every
   in-tree consumer — ``src/``, ``tests/``, ``scripts/`` — was migrated to
   ``notebooklm._auth.browser_capture`` in the same PR that created this file.
"""

from __future__ import annotations

from .browser_capture import heal_captured_state

__all__ = ["heal_captured_state"]
