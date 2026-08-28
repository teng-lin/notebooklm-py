"""Lazy compatibility shim for the public web-only research namespace.

The implementation moved whole to :mod:`notebooklm._web.research`. Keeping
resolution lazy avoids making this compatibility path a second composition
root while preserving every historical name as the exact implementation
object.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [  # noqa: F822 - resolved lazily below
    "CitedSourceSelection",
    "ResearchAPI",
    "ResearchSource",
    "ResearchStart",
    "ResearchStatus",
    "ResearchTask",
]

_IMPLEMENTATION = "notebooklm._web.research"


def __getattr__(name: str) -> Any:
    implementation = import_module(_IMPLEMENTATION)
    try:
        value = getattr(implementation, name)
    except AttributeError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    implementation = import_module(_IMPLEMENTATION)
    return sorted(set(globals()) | set(dir(implementation)))
