"""Deprecated shim — the canonical storage writer now lives in ``storage.py``.

ADR-0033's persistence merge folded this module (and ``storage_transaction.py``)
into :mod:`notebooklm._auth.storage`, so the ``storage_state.json`` seam is one
deep module instead of three cap-split files. Every name below is a re-export of
its new home; nothing is defined here.

.. warning::
   **Removal: the next major release.** This shim exists only so an out-of-tree
   importer of ``notebooklm._auth.storage_writer`` (a private module, so this is
   already outside the supported surface) does not break mid-minor. Every in-tree
   consumer — ``src/``, ``tests/``, ``scripts/`` — was migrated to
   ``notebooklm._auth.storage`` in the same PR that created this file. Import
   from :mod:`notebooklm._auth.storage`, or better, from the ``notebooklm.auth``
   facade.
"""

from __future__ import annotations

from .storage import (
    CLEAR_ACCOUNT,
    KEEP_ACCOUNT,
    AccountArg,
    AccountRecord,
    LockUnavailableError,
    LoginWriteOutcome,
    LoginWriteStatus,
    WriteOutcome,
    WriteStatus,
    clear_in_band_account,
    merge_cookie_delta,
    persist_minted_jar,
    replace_from_login,
    replace_from_remint,
    update_account_metadata,
    write_master_token,
)

__all__ = [
    "CLEAR_ACCOUNT",
    "KEEP_ACCOUNT",
    "AccountArg",
    "AccountRecord",
    "LockUnavailableError",
    "LoginWriteOutcome",
    "LoginWriteStatus",
    "WriteOutcome",
    "WriteStatus",
    "clear_in_band_account",
    "merge_cookie_delta",
    "persist_minted_jar",
    "replace_from_login",
    "replace_from_remint",
    "update_account_metadata",
    "write_master_token",
]
