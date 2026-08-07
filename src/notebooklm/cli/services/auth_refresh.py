"""Click-free orchestration helpers for ``notebooklm auth refresh``."""

from __future__ import annotations

from pathlib import Path

from ...auth import BootstrapOutcome, master_token_bootstrap_storage


async def bootstrap_missing_storage_from_master_token(storage_path: Path) -> bool:
    """Mint initial storage when only the sibling master token exists.

    Thin call into the bootstrap transaction (#2103 PR-2 D7 — the flock /
    shield / recheck machinery that used to live here now lives in
    :func:`notebooklm._auth.master_token.bootstrap_storage_from_master_token`,
    reached only through the public ``notebooklm.auth`` facade since ``cli/``
    may not import ``notebooklm._*``). Maps the four-state
    :class:`~notebooklm.auth.BootstrapOutcome` onto the boolean this caller
    (``cli/playwright_login_io.py``) has always used: ``MINTED`` and
    ``PRESENT_AFTER_WAIT`` both mean "storage is ready, take the mandatory
    passive-validation path"; ``PRESENT_ON_ENTRY`` and ``NO_TOKEN`` both mean
    "nothing was bootstrapped here, enter ordinary recovery" — exactly
    today's semantics, made explicit rather than collapsed into one bool
    before it ever reaches this module.
    """
    outcome = await master_token_bootstrap_storage(storage_path)
    return outcome in (BootstrapOutcome.MINTED, BootstrapOutcome.PRESENT_AFTER_WAIT)


__all__ = ["bootstrap_missing_storage_from_master_token"]
