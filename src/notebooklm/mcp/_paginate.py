"""Bounded pagination for MCP list tools.

The ``*_list`` tools return a whole collection; on a large account or notebook
that can be a big payload that burns agent context. :func:`paginate` slices to a
``limit`` and reports ``total`` / ``has_more`` so the agent sees a bounded page
and knows whether to ask for more.

The pure slice + bound-validation now lives in the transport-neutral
:func:`notebooklm._app.pagination.paginate` (shared with the REST list routes,
Option B-lite); this module re-exports it and pins the MCP-specific default page
size. (ponytail: client-side slice; push paging into the RPC layer only if list
sizes ever make the fetch itself the bottleneck.)

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from .._app.pagination import paginate

__all__ = ["DEFAULT_LIMIT", "paginate"]

#: Default page size for the ``*_list`` tools when the caller omits ``limit``.
DEFAULT_LIMIT = 50
