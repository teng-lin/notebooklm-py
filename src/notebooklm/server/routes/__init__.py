"""REST route modules for the single-tenant server.

Each module exposes a ``router`` (a FastAPI ``APIRouter``) the application
factory mounts under ``/v1``. The modules are thin adapters over the
transport-neutral ``_app`` cores and the public client namespaces; they import
NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from . import artifacts, chat, meta, notebooks, notes, research, share, sources

__all__ = [
    "artifacts",
    "chat",
    "meta",
    "notebooks",
    "notes",
    "research",
    "share",
    "sources",
]
