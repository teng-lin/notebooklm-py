"""Static Android profile configuration and cookie-free client construction."""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from pathlib import Path

from ..paths import get_storage_path


def profile_startup_timeout(env_name: str) -> float:
    """Bound each multi-profile construction attempt, including recovery."""
    raw = os.environ.get(env_name, "").strip() or "30"
    try:
        timeout = float(raw)
    except ValueError:
        raise ValueError(f"{env_name} must be positive finite seconds") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{env_name} must be positive finite seconds")
    return timeout


def configured_profiles(profiles: Sequence[str]) -> dict[str, Path]:
    """Resolve once and reject aliases before any client reads credentials."""
    if isinstance(profiles, str) or not profiles:
        raise ValueError("profiles must be a non-empty sequence of profile names")
    resolved: dict[str, Path] = {}
    paths: set[str] = set()
    names: set[str] = set()
    for name in profiles:
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError("Profile names must be non-empty and have no surrounding whitespace")
        if any(ord(char) < 33 or ord(char) > 126 for char in name) or "," in name:
            raise ValueError("Profile names must be printable ASCII without spaces or commas")
        path = get_storage_path(name).resolve()
        # Reject case-only aliases portably, including symlink targets whose
        # resolved spelling can differ on a case-insensitive filesystem.
        path_key = str(path).casefold()
        if name.casefold() in names or path_key in paths:
            raise ValueError(f"Duplicate profile or canonical storage path: {name!r}")
        resolved[name] = path
        names.add(name.casefold())
        paths.add(path_key)
    return resolved
