"""Shared VCR-replay client helper for the golden decoded-row modules.

Used by ``test_golden_decoded_vcr.py`` and ``test_golden_decoded_vcr_expansion.py``
(extracted, like ``_golden_assert.py``, so the two modules don't cross-import —
see the ``tests/_guardrails/test_no_cross_test_imports.py`` gate).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from tests.integration.conftest import get_vcr_auth

from notebooklm import NotebookLMClient


@asynccontextmanager
async def vcr_client():
    """Authenticated client bound to VCR replay (mock auth in replay mode)."""
    auth = await get_vcr_auth()
    async with NotebookLMClient(auth) as client:
        yield client
