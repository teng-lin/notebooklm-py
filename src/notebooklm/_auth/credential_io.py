"""Sealed typed commit capability for authentication credentials."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .._atomic_io import _atomic_write_json_unchecked


def _commit_json_unchecked(path: Path, payload: Mapping[str, object]) -> None:
    """Forward one credential payload to the guarded atomic capability."""
    _atomic_write_json_unchecked(path, payload)


def _commit_profile_json(path: Path, payload: Mapping[str, object]) -> None:
    """Commit one complete profile document without changing its representation."""
    _commit_json_unchecked(path, payload)


def _commit_master_token_json(path: Path, payload: Mapping[str, object]) -> None:
    """Commit one complete master-token document without changing its representation."""
    _commit_json_unchecked(path, payload)
