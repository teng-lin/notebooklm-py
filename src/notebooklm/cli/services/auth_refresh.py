"""Click-free orchestration helpers for ``notebooklm auth refresh``."""

from __future__ import annotations

from pathlib import Path

from .login import master_token


async def bootstrap_missing_storage_from_master_token(storage_path: Path) -> bool:
    """Mint initial storage when only the sibling master token exists."""
    if storage_path.exists():
        return False
    master_token_path = storage_path.parent / "master_token.json"
    if not master_token_path.exists():
        return False
    await master_token.refresh(
        storage_path=storage_path,
        master_token_path=master_token_path,
    )
    return True


__all__ = ["bootstrap_missing_storage_from_master_token"]
