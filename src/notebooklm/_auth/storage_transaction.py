"""Deprecated shim — the storage-write transaction template now lives in ``storage.py``.

ADR-0033's persistence merge folded this module (and ``storage_writer.py``) into
:mod:`notebooklm._auth.storage`. The template was only ever split out of the
writer to stay under the ADR-0008 module-size budget, and it lazily imported the
lock primitives back from the module it was split from; both halves now sit in
one module and that lazy import is gone. Every name below is a re-export of its
new home; nothing is defined here.

.. warning::
   **Removal: the next major release.** This shim exists only so an out-of-tree
   importer of ``notebooklm._auth.storage_transaction`` (a private module, so
   this is already outside the supported surface) does not break mid-minor. Every
   in-tree consumer was migrated to ``notebooklm._auth.storage`` in the same PR
   that created this file.
"""

from __future__ import annotations

from .storage import (
    in_storage_transaction,
    raise_on_lock_unavailable,
    report_on_lock_unavailable,
    skip_on_lock_unavailable,
)

__all__ = [
    "in_storage_transaction",
    "raise_on_lock_unavailable",
    "report_on_lock_unavailable",
    "skip_on_lock_unavailable",
]
