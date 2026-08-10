"""Deprecated shim — the write-time cookie-domain filter now lives in ``storage.py``.

ADR-0033 PR 4.2 relocated this module's content into
:mod:`notebooklm._auth.storage`, beside the intent writers that apply it. The
filter was never browser code: three of its six call sites are writer intents
(``replace_from_remint`` / ``replace_from_login`` / ``persist_minted_jar``), each
of which had to reach it through a function-local import from this leaf. It is
write-time policy that happened to carry a ``browser_`` name because the two
capture arms were its first callers. Every name below is a re-export of its new
home; nothing is defined here.

Note that ``notebooklm._auth.browser_capture`` still re-exports the filter (and
``_safe_cookie_shape``) under its own names — that is the CLI's sanctioned path
to it (``cli/services/playwright_login.py``), not a shim, and it is unaffected by
this file's removal.

.. warning::
   **Removal: the next major release.** This shim exists only so an out-of-tree
   importer of ``notebooklm._auth._browser_cookie_filter`` (a private module, so
   this is already outside the supported surface) does not break mid-minor. Every
   in-tree consumer — ``src/``, ``tests/``, ``scripts/`` — was migrated to
   ``notebooklm._auth.storage`` in the same PR that created this file.
"""

from __future__ import annotations

from .storage import filter_storage_state_cookies_by_domain_policy

__all__ = ["filter_storage_state_cookies_by_domain_policy"]
