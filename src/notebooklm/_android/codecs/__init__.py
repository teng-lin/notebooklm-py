"""Lazy Android protobuf-to-public-type codec compatibility exports.

Importing a selected adapter must not execute unrelated generated-protobuf
imports during client construction. Concrete adapters import their codec
submodules directly; ``__getattr__`` retains the older package-level private
aliases without making this package initializer an eager dependency fan-out.
"""

from __future__ import annotations

from typing import Any

_NOTEBOOK_EXPORTS = {"decode_project", "map_get_project_error", "message_to_known_dict"}
_SOURCE_EXPORTS = {"decode_source", "decode_sources"}


def __getattr__(name: str) -> Any:
    if name in _NOTEBOOK_EXPORTS:
        from . import notebooks

        return getattr(notebooks, name)
    if name in _SOURCE_EXPORTS:
        from . import sources

        return getattr(sources, name)
    raise AttributeError(name)


__all__ = [
    "decode_project",
    "decode_source",
    "decode_sources",
    "map_get_project_error",
    "message_to_known_dict",
]
