"""Deprecated shim — the login-wait DEBUG tracing now lives in ``browser_capture.py``.

ADR-0033's browser-cluster merge (PR 4.1) folded this module into
:mod:`notebooklm._auth.browser_capture`. Its own docstring said outright that it
was split out "so the capture core stays under the ADR-0008 module-size budget",
and ``browser_capture`` was its only consumer (three call sites). Every name below
is a re-export of its new home; nothing is defined here.

Note that :func:`trace_url` deliberately survives the merge as a **second** URL
redactor alongside ``_auth.extraction._safe_url`` — see its docstring in
``browser_capture`` for why the two policies must stay distinct.

.. warning::
   **Removal: the next major release.** This shim exists only so an out-of-tree
   importer of ``notebooklm._auth.login_wait_trace`` (a private module, so this is
   already outside the supported surface) does not break mid-minor. Every in-tree
   consumer — ``src/``, ``tests/``, ``scripts/`` — was migrated to
   ``notebooklm._auth.browser_capture`` in the same PR that created this file.

   Log records now carry the ``notebooklm._auth.browser_capture`` logger name
   rather than ``notebooklm._auth.login_wait_trace``: the tracing helpers use
   their defining module's logger, and that module changed. Anything filtering
   ``-vv`` output by logger name must follow.
"""

from __future__ import annotations

from .browser_capture import log_observed_navigations, safe_page_url, trace_url

__all__ = ["log_observed_navigations", "safe_page_url", "trace_url"]
