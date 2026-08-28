"""Lazy compatibility shim for the web-only source-label namespace."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["LabelsAPI", "ListSources"]  # noqa: F822 - resolved lazily below

_IMPLEMENTATION = "notebooklm._web.labels"


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
