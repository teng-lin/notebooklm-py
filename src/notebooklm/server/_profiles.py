"""Static Android profile configuration and cookie-free client construction."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..paths import get_storage_path

PROFILES_ENV = "NOTEBOOKLM_SERVER_PROFILES"
PROFILE_HEADER = "X-NotebookLM-Profile"


def configured_profiles(profiles: Sequence[str]) -> dict[str, Path]:
    """Resolve once and reject aliases before any client reads credentials."""
    if isinstance(profiles, str) or not profiles:
        raise ValueError("profiles must be a non-empty sequence of profile names")
    resolved: dict[str, Path] = {}
    paths: set[Path] = set()
    for name in profiles:
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError("Profile names must be non-empty and have no surrounding whitespace")
        if any(ord(char) < 33 or ord(char) > 126 for char in name) or "," in name:
            raise ValueError("Profile names must be printable ASCII without spaces or commas")
        path = get_storage_path(name).resolve()
        if name in resolved or path in paths:
            raise ValueError(f"Duplicate profile or canonical storage path: {name!r}")
        resolved[name] = path
        paths.add(path)
    return resolved
